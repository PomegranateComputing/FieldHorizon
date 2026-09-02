from __future__ import annotations

import logging
import math

import requests
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from .config import AppConfig
from .db import connect
from .llm import embed as embed_text
from .storage_vectors import VectorStore, blob_to_vector, vector_to_blob

logger = logging.getLogger(__name__)

_embedding_version_cache: dict[tuple[str, str], str] = {}


def current_embedding_version(cfg: AppConfig) -> str:
    """
    Model name + revision string (standing rule: vectors from different
    embedding versions must never be compared). Resolves the configured
    embedding model's digest via Ollama's /api/tags, so a model re-pulled
    under the same tag with different weights doesn't silently compare
    against vectors embedded before the pull. Cached in-process per
    (base_url, model) -- one network round trip per process, not per call.

    Falls back to the bare model name, logged, if the digest can't be
    resolved (Ollama unreachable, model not listed). This keeps the
    pipeline running rather than failing every embedding call; it does not
    weaken the comparability guarantee, since a fallback version is still
    a single consistent string that every comparison site keys against --
    it just means a version change during an Ollama outage wouldn't be
    detected until the digest lookup succeeds again.
    """
    cache_key = (cfg.ollama_base_url, cfg.embedding_model)
    if cache_key in _embedding_version_cache:
        return _embedding_version_cache[cache_key]

    version = cfg.embedding_model

    try:
        url = cfg.ollama_base_url.rstrip("/") + "/api/tags"
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        for entry in res.json().get("models", []):
            name = str(entry.get("name", ""))
            if name == cfg.embedding_model or name.split(":")[0] == cfg.embedding_model:
                digest = str(entry.get("digest", ""))
                if digest:
                    version = f"{cfg.embedding_model}@{digest[:12]}"
                break
    except Exception as exc:
        logger.warning(
            "Could not resolve embedding_version digest for %s, using bare model name: %s",
            cfg.embedding_model,
            exc,
        )

    _embedding_version_cache[cache_key] = version
    return version


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0

    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (norm_a * norm_b)


def store_cycle_embedding(cfg: AppConfig, cycle_id: int, vector: list[float], model: str, embedding_version: str) -> None:
    with connect(cfg.database) as conn:
        conn.execute(
            """
            INSERT INTO canon_embeddings(cycle_id, vector, dim, model, embedding_version)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cycle_id) DO UPDATE SET
                vector = excluded.vector,
                dim = excluded.dim,
                model = excluded.model,
                embedding_version = excluded.embedding_version,
                created_at = CURRENT_TIMESTAMP
            """,
            (cycle_id, vector_to_blob(vector), len(vector), model, embedding_version),
        )
        conn.commit()


def embed_and_store_fragment(cfg: AppConfig, cycle_id: int, fragment: str) -> None:
    """
    Embed a canon fragment at write time. Best-effort: an embedding failure
    (e.g. the configured embedding model isn't pulled) must not fail the
    cycle write it's attached to -- MMR selection and backfill both treat a
    missing vector as "not yet embedded" and degrade gracefully.
    """
    try:
        vector = embed_text(cfg, fragment)
    except Exception as exc:
        logger.warning("Embedding fragment for cycle %d failed: %s", cycle_id, exc)
        return

    store_cycle_embedding(cfg, cycle_id, vector, cfg.embedding_model, current_embedding_version(cfg))


def load_cycle_embedding(cfg: AppConfig, cycle_id: int) -> list[float] | None:
    """
    Returns None if no vector is stored, OR if the stored vector's
    embedding_version doesn't match the currently configured one --
    vectors from different versions must never be compared (standing
    rule), so a version mismatch is treated identically to "not embedded
    yet" by every caller (MMR selection, backfill).
    """
    with connect(cfg.database) as conn:
        row = conn.execute(
            "SELECT vector, embedding_version FROM canon_embeddings WHERE cycle_id = ?",
            (cycle_id,),
        ).fetchone()

    if row is None:
        return None
    if row["embedding_version"] != current_embedding_version(cfg):
        return None

    return blob_to_vector(row["vector"])


def backfill_canon_embeddings(cfg: AppConfig, limit: int | None = None) -> int:
    """
    Embed every CANON fragment that reached canon before embeddings existed
    (or whose write-time embedding attempt failed), plus any fragment whose
    stored vector is from a stale embedding_version. Idempotent: only
    considers cycles with no *current-version* vector in canon_embeddings.
    """
    version = current_embedding_version(cfg)
    query = """
        SELECT c.id, c.fragment
        FROM cycles c
        LEFT JOIN canon_embeddings e ON e.cycle_id = c.id AND e.embedding_version = ?
        WHERE c.verdict = 'CANON'
          AND c.dry_run = 0
          AND c.fragment IS NOT NULL
          AND c.fragment != ''
          AND e.cycle_id IS NULL
        ORDER BY c.created_at DESC
    """
    params: tuple = (version,)
    if limit is not None:
        query += " LIMIT ?"
        params = (version, limit)

    with connect(cfg.database) as conn:
        rows = conn.execute(query, params).fetchall()

    count = 0
    for row in rows:
        cycle_id = int(row["id"])
        embed_and_store_fragment(cfg, cycle_id, row["fragment"])
        if load_cycle_embedding(cfg, cycle_id) is not None:
            count += 1

    return count


def store_chunk_embedding(
    cfg: AppConfig, chunk_id: int, vector: list[float], model: str, embedding_version: str, store: VectorStore | None = None
) -> None:
    (store or VectorStore(cfg, "chunk")).upsert(chunk_id, vector, embedding_version)


def embed_and_store_chunk(cfg: AppConfig, chunk_id: int, content: str, store: VectorStore | None = None) -> None:
    """
    Embed a book/manifesto chunk at ingest time. Best-effort, same as
    embed_and_store_fragment: a missing embedding model must not fail
    ingestion, it just means this chunk isn't reachable by
    search_books_by_embedding until backfill_chunk_embeddings runs.

    `store` lets a batched caller (backfill, ingest-manifest) share one
    VectorStore across many chunks instead of re-probing the backend and
    re-checking migration state per row.
    """
    try:
        vector = embed_text(cfg, content)
    except Exception as exc:
        logger.warning("Embedding chunk %d failed: %s", chunk_id, exc)
        return

    store_chunk_embedding(cfg, chunk_id, vector, cfg.embedding_model, current_embedding_version(cfg), store=store)


def load_all_chunk_embeddings(cfg: AppConfig) -> dict[int, list[float]]:
    """
    Only returns vectors matching the current embedding_version -- a
    stale-version vector is indistinguishable from "not embedded yet" to
    every caller, since comparing across versions is never valid.
    """
    return VectorStore(cfg, "chunk").all_vectors(current_embedding_version(cfg))


def backfill_chunk_embeddings(cfg: AppConfig, limit: int | None = None, show_progress: bool = True) -> int:
    """
    Embed every chunk that predates the embedding model, or whose
    ingest-time embedding attempt failed, or whose stored vector is from a
    stale embedding_version. Idempotent and resumable: only considers chunks
    with no *current-version* row (VectorStore.missing_keys against the
    durable BLOB table), so an interrupted run picks back up exactly where
    it left off rather than re-embedding already-done chunks.
    """
    version = current_embedding_version(cfg)
    store = VectorStore(cfg, "chunk")

    with connect(cfg.database) as conn:
        all_ids = [int(row["id"]) for row in conn.execute("SELECT id FROM chunks ORDER BY id ASC").fetchall()]

    missing_ids = store.missing_keys(all_ids, version)
    if limit is not None:
        missing_ids = missing_ids[:limit]
    if not missing_ids:
        return 0

    with connect(cfg.database) as conn:
        placeholders = ",".join("?" for _ in missing_ids)
        rows = conn.execute(
            f"SELECT id, content FROM chunks WHERE id IN ({placeholders})", missing_ids
        ).fetchall()
    content_by_id = {int(row["id"]): row["content"] for row in rows}

    count = 0
    with _embedding_progress(disable=not show_progress) as progress:
        task = progress.add_task("Embedding chunks", total=len(missing_ids))
        for chunk_id in missing_ids:
            embed_and_store_chunk(cfg, chunk_id, content_by_id[chunk_id], store=store)
            if store.get(chunk_id, version) is not None:
                count += 1
            progress.advance(task)

    return count


def backfill_axiom_embeddings(cfg: AppConfig, limit: int | None = None, show_progress: bool = True) -> int:
    """
    Embed every json_entries axiom statement not yet embedded at the current
    embedding_version -- proactive, batched counterpart to
    doctrinal.get_axiom_vector's lazy per-evaluation embed. Idempotent and
    resumable via the same VectorStore.missing_keys primitive used for
    chunks.
    """
    version = current_embedding_version(cfg)
    store = VectorStore(cfg, "axiom")

    with connect(cfg.database) as conn:
        rows = conn.execute("SELECT id, statement FROM json_entries ORDER BY id ASC").fetchall()
    statement_by_id = {row["id"]: row["statement"] for row in rows}

    missing_ids = store.missing_keys(list(statement_by_id.keys()), version)
    if limit is not None:
        missing_ids = missing_ids[:limit]
    if not missing_ids:
        return 0

    count = 0
    with _embedding_progress(disable=not show_progress) as progress:
        task = progress.add_task("Embedding axioms", total=len(missing_ids))
        for axiom_id in missing_ids:
            statement = statement_by_id[axiom_id]
            try:
                vector = embed_text(cfg, statement)
            except Exception as exc:
                logger.warning("Embedding axiom %s failed: %s", axiom_id, exc)
                progress.advance(task)
                continue
            store.upsert(axiom_id, vector, version)
            count += 1
            progress.advance(task)

    return count


def _embedding_progress(disable: bool = False) -> Progress:
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        disable=disable,
    )
