from __future__ import annotations

import array
import logging
import re
import sqlite3
from typing import Literal

from .config import AppConfig
from .db import connect

logger = logging.getLogger(__name__)


def vector_to_blob(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def blob_to_vector(blob: bytes) -> list[float]:
    arr = array.array("f")
    arr.frombytes(blob)
    return list(arr)


Family = Literal["chunk", "axiom"]

_FAMILY_TABLES: dict[Family, dict[str, str]] = {
    "chunk": {"blob_table": "chunk_embeddings", "key_col": "chunk_id", "key_type": "INTEGER", "vec_table": "chunk_vectors_vec0"},
    "axiom": {"blob_table": "axiom_embeddings", "key_col": "id", "key_type": "TEXT", "vec_table": "axiom_vectors_vec0"},
}

_vec_available: bool | None = None


def vec_extension_available() -> bool:
    """
    Whether the sqlite-vec loadable extension is importable and actually
    loads into a real SQLite connection. Probed once and cached for the
    process -- this can't change mid-run, so there's nothing to keep
    re-checking. `field-horizon` has exactly two vector backends (sqlite-vec
    vec0 tables, or BLOB + numpy); this is the sole switch between them.
    """
    global _vec_available
    if _vec_available is not None:
        return _vec_available

    try:
        import sqlite_vec

        probe = sqlite3.connect(":memory:")
        probe.enable_load_extension(True)
        sqlite_vec.load(probe)
        probe.enable_load_extension(False)
        probe.close()
        _vec_available = True
    except Exception as exc:
        logger.info("sqlite-vec extension unavailable, using BLOB + numpy fallback: %s", exc)
        _vec_available = False

    return _vec_available


def _vec_connection(cfg: AppConfig) -> sqlite3.Connection:
    import sqlite_vec

    conn = connect(cfg.database)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


class VectorStore:
    """
    Vector storage seam for one key family ("chunk": INTEGER chunk ids, or
    "axiom": TEXT json_entries ids). The pre-existing chunk_embeddings /
    axiom_embeddings BLOB tables remain the single durable source of truth
    and the sole basis for resumability (missing_keys), regardless of which
    backend is active -- a sqlite-vec vec0 table, when available, is layered
    on top purely as a faster search index and is kept in sync on every
    upsert. This means a process that starts without the extension loadable
    still sees every previously embedded vector via the BLOB path, and a
    process that starts WITH it available transparently adopts any vectors
    written by a prior BLOB-only run (sqlite_vec.serialize_float32 produces
    byte-identical output to this project's own vector_to_blob, confirmed
    empirically, so migration is a byte copy, never a re-embed).
    """

    def __init__(self, cfg: AppConfig, family: Family, backend: Literal["auto", "vec0", "blob"] = "auto"):
        if family not in _FAMILY_TABLES:
            raise ValueError(f"unknown vector family: {family!r}")

        spec = _FAMILY_TABLES[family]
        self.cfg = cfg
        self.family = family
        self.blob_table = spec["blob_table"]
        self.key_col = spec["key_col"]
        self.key_type = spec["key_type"]
        self.vec_table = spec["vec_table"]

        if backend == "auto":
            backend = "vec0" if vec_extension_available() else "blob"
        self.backend = backend

        if self.backend == "vec0":
            self._migrate_legacy_blob_rows()

    def _existing_vec_table_dim(self, conn: sqlite3.Connection) -> int | None:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (self.vec_table,)).fetchone()
        if row is None or row["sql"] is None:
            return None
        match = re.search(r"FLOAT\[(\d+)\]", row["sql"])
        return int(match.group(1)) if match else None

    def _ensure_vec_table_for_dim(self, conn: sqlite3.Connection, dim: int) -> bool:
        """
        Vectors have no fixed dimension across the whole install -- it's
        whatever the configured embedding model emits, and vec0 needs that
        dimension declared in the column type at CREATE TABLE time. So the
        table is (re)created lazily against the dimension actually seen,
        rather than assuming one. A dimension change only ever happens
        alongside an embedding_version change (a different model), which
        already means every existing row is stale under the version
        discipline -- dropping the old vec0 table costs nothing but its role
        as a cache; the durable BLOB table is untouched. Returns True if the
        table was (re)created.
        """
        existing_dim = self._existing_vec_table_dim(conn)
        if existing_dim == dim:
            return False

        conn.execute(f"DROP TABLE IF EXISTS {self.vec_table}")
        conn.execute(
            f"CREATE VIRTUAL TABLE {self.vec_table} USING vec0("
            f"id {self.key_type} PRIMARY KEY, "
            f"embedding FLOAT[{dim}] distance_metric=cosine, "
            f"+embedding_version TEXT)"
        )
        conn.commit()
        return True

    def _migrate_legacy_blob_rows(self) -> None:
        with connect(self.cfg.database) as plain:
            sample = plain.execute(f"SELECT dim FROM {self.blob_table} LIMIT 1").fetchone()

        if sample is None:
            return  # nothing embedded yet; the vec0 table is created lazily on first upsert

        dim = int(sample["dim"])

        with _vec_connection(self.cfg) as conn:
            recreated = self._ensure_vec_table_for_dim(conn, dim)
            if not recreated:
                blob_count_row = conn.execute(f"SELECT COUNT(*) AS n FROM {self.blob_table}").fetchone()
                vec_count_row = conn.execute(f"SELECT COUNT(*) AS n FROM {self.vec_table}").fetchone()
                if blob_count_row["n"] == vec_count_row["n"]:
                    # Common case on an up-to-date process: a couple of
                    # COUNT(*) queries instead of a full row scan every time
                    # a VectorStore is constructed.
                    return

            with connect(self.cfg.database) as plain:
                legacy_rows = plain.execute(
                    f"SELECT {self.key_col} AS key, vector, embedding_version FROM {self.blob_table}"
                ).fetchall()

            existing_keys = {row["id"] for row in conn.execute(f"SELECT id FROM {self.vec_table}").fetchall()}
            copied = 0
            for row in legacy_rows:
                if row["key"] in existing_keys or len(row["vector"]) // 4 != dim:
                    continue
                conn.execute(
                    f"INSERT INTO {self.vec_table}(id, embedding, embedding_version) VALUES (?, ?, ?)",
                    (row["key"], row["vector"], row["embedding_version"]),
                )
                copied += 1
            conn.commit()

        if copied:
            logger.info("storage_vectors: migrated %d legacy %s vector(s) into %s", copied, self.family, self.vec_table)

    def upsert(self, key: int | str, vector: list[float], embedding_version: str) -> None:
        blob = vector_to_blob(vector)
        with connect(self.cfg.database) as conn:
            conn.execute(
                f"""
                INSERT INTO {self.blob_table}({self.key_col}, vector, dim, model, embedding_version)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT({self.key_col}) DO UPDATE SET
                    vector = excluded.vector, dim = excluded.dim,
                    model = excluded.model, embedding_version = excluded.embedding_version,
                    created_at = CURRENT_TIMESTAMP
                """,
                (key, blob, len(vector), self.cfg.embedding_model, embedding_version),
            )
            conn.commit()

        if self.backend == "vec0":
            with _vec_connection(self.cfg) as conn:
                self._ensure_vec_table_for_dim(conn, len(vector))
                # vec0 tables reject INSERT OR REPLACE (UNIQUE constraint on
                # the primary key); confirmed live -- delete-then-insert is
                # the only upsert path that works.
                conn.execute(f"DELETE FROM {self.vec_table} WHERE id = ?", (key,))
                conn.execute(
                    f"INSERT INTO {self.vec_table}(id, embedding, embedding_version) VALUES (?, ?, ?)",
                    (key, blob, embedding_version),
                )
                conn.commit()

    def get(self, key: int | str, embedding_version: str) -> list[float] | None:
        with connect(self.cfg.database) as conn:
            row = conn.execute(
                f"SELECT vector FROM {self.blob_table} WHERE {self.key_col} = ? AND embedding_version = ?",
                (key, embedding_version),
            ).fetchone()
        return blob_to_vector(row["vector"]) if row is not None else None

    def missing_keys(self, all_keys: list, embedding_version: str) -> list:
        """
        Keys with no row at the current embedding_version in the BLOB table
        -- the resumability primitive, checked against the BLOB table always
        (it's the source of truth), independent of which backend is active.
        """
        if not all_keys:
            return []
        placeholders = ",".join("?" for _ in all_keys)
        with connect(self.cfg.database) as conn:
            existing = {
                row[self.key_col]
                for row in conn.execute(
                    f"SELECT {self.key_col} FROM {self.blob_table} "
                    f"WHERE embedding_version = ? AND {self.key_col} IN ({placeholders})",
                    (embedding_version, *all_keys),
                ).fetchall()
            }
        return [key for key in all_keys if key not in existing]

    def vectors_for_keys(self, keys: list, embedding_version: str) -> dict:
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        with connect(self.cfg.database) as conn:
            rows = conn.execute(
                f"SELECT {self.key_col} AS key, vector FROM {self.blob_table} "
                f"WHERE embedding_version = ? AND {self.key_col} IN ({placeholders})",
                (embedding_version, *keys),
            ).fetchall()
        return {row["key"]: blob_to_vector(row["vector"]) for row in rows}

    def all_vectors(self, embedding_version: str) -> dict:
        """All current-version vectors for this family, keyed by id. Used by
        fingerprint computation (mean chunk embedding per source) rather than
        by search, which stays keyed off a single query vector."""
        with connect(self.cfg.database) as conn:
            rows = conn.execute(
                f"SELECT {self.key_col} AS key, vector FROM {self.blob_table} WHERE embedding_version = ?",
                (embedding_version,),
            ).fetchall()
        return {row["key"]: blob_to_vector(row["vector"]) for row in rows}

    def search(
        self,
        query_vector: list[float],
        embedding_version: str,
        limit: int,
        exclude_keys: set | None = None,
    ) -> list[tuple]:
        """Returns up to `limit` (key, cosine_similarity) pairs, best first."""
        exclude_keys = exclude_keys or set()

        if self.backend == "vec0":
            return self._search_vec0(query_vector, embedding_version, limit, exclude_keys)
        return self._search_blob(query_vector, embedding_version, limit, exclude_keys)

    def _search_vec0(self, query_vector, embedding_version, limit, exclude_keys) -> list[tuple]:
        import sqlite_vec

        # vec0 KNN queries can SELECT an auxiliary column (embedding_version)
        # but cannot WHERE-filter on one alongside `k`, confirmed live -- so
        # version/exclusion filtering happens in Python after an overfetch.
        overfetch = limit + len(exclude_keys) + 50
        with _vec_connection(self.cfg) as conn:
            if self._existing_vec_table_dim(conn) is None:
                return []  # nothing has ever been upserted through the vec0 path yet
            rows = conn.execute(
                f"SELECT id, distance, embedding_version FROM {self.vec_table} "
                f"WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (sqlite_vec.serialize_float32(query_vector), overfetch),
            ).fetchall()

        results: list[tuple] = []
        for row in rows:
            if row["embedding_version"] != embedding_version or row["id"] in exclude_keys:
                continue
            results.append((row["id"], 1.0 - row["distance"]))
            if len(results) >= limit:
                break
        return results

    def _search_blob(self, query_vector, embedding_version, limit, exclude_keys) -> list[tuple]:
        import numpy as np

        with connect(self.cfg.database) as conn:
            rows = conn.execute(
                f"SELECT {self.key_col} AS key, vector FROM {self.blob_table} WHERE embedding_version = ?",
                (embedding_version,),
            ).fetchall()

        candidates = [(row["key"], row["vector"]) for row in rows if row["key"] not in exclude_keys]
        if not candidates:
            return []

        query = np.array(query_vector, dtype=np.float32)
        query_norm = float(np.linalg.norm(query))
        if query_norm == 0.0:
            return []

        matrix = np.array([blob_to_vector(vec) for _, vec in candidates], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0.0] = 1.0  # rows with a zero vector score 0 similarity via a zero dot product

        similarities = (matrix @ query) / (norms * query_norm)
        order = np.argsort(-similarities)[:limit]
        return [(candidates[i][0], float(similarities[i])) for i in order]
