"""
Bridging harvested documents into Field Horizon's existing ingestion.

This module writes `sources` and `chunks` rows in exactly the shape
`ingest._ingest_manifest_entry` already writes them, using the same
`chunk_text`. That is the whole design: retrieval, hybrid scoring,
embeddings, concept tagging, and fingerprinting then work on harvested
documents with **no changes at all** to those subsystems.

Three details make it correct rather than merely similar:

* **Idempotence by `manifest_id`.** Every harvested source gets
  `manifest_id = "corpus:<document_id>"`, and `sources` already has a
  partial unique index on that column. A second run over an unchanged
  document finds the row and does nothing. A document whose *text* has
  changed (a re-normalization, an upstream correction) is detected by
  comparing the stored normalized hash and is re-chunked.
* **FTS hygiene.** `chunks_fts` has no foreign key to `chunks`, so old
  FTS rows must be deleted explicitly before the chunks they indexed
  disappear -- the same discipline `_delete_stale_fts_for_source`
  enforces for the existing paths. Skipping it accumulates ghost search
  results forever.
* **Untrusted-content marking.** Each chunk's `metadata` JSON carries
  `untrusted_content: true` alongside the document id, licence, and
  source URL. Retrieval reads it back, so a chunk of harvested text
  arrives at the prompt boundary already labelled as third-party data
  rather than as instruction.

`source_type` is the singular `"book"` or `"manifesto"` -- the values the
existing ontology routing in `retrieval.wanted_sources` already knows.
Inventing a new source_type would make harvested documents invisible to
domain routing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import AppConfig
from ..db import connect
from ..ingest import chunk_text
from .models import Destination
from .repository import CorpusRepository, ItemRecord

logger = logging.getLogger(__name__)

#: Prefix distinguishing harvester-owned `sources` rows from every other
#: kind. Every corpus row is separable by this, which is what makes a
#: full de-indexing possible without touching hand-curated sources.
MANIFEST_PREFIX = "corpus:"

CHUNKER_VERSION = "fieldhorizon.ingest.chunk_text/1"

#: `sources.source_type` values. Deliberately the ones retrieval already
#: routes on, not new ones.
SOURCE_TYPE_BY_DESTINATION = {
    Destination.BOOKS: "book",
    Destination.MANIFESTO: "manifesto",
}


@dataclass(frozen=True)
class IngestOutcome:
    document_id: str
    status: str  # "ingested" | "unchanged" | "reingested" | "skipped"
    source_id: int | None
    chunk_count: int
    reason: str = ""


def manifest_id_for(document_id: str) -> str:
    return f"{MANIFEST_PREFIX}{document_id}"


def _source_weight(item: ItemRecord) -> float:
    """
    Retrieval trust for a harvested source.

    Capped below 1.0 on purpose: hand-curated corpus entries default to
    weight 1.0, and an automatically acquired text should not outrank a
    deliberately chosen one by default. Quality modulates within that
    ceiling, so a pristine Standard Ebooks text still beats a marginal
    OCR extraction.
    """
    quality = item.quality_score if item.quality_score is not None else 0.5
    return round(0.5 + 0.4 * max(0.0, min(1.0, quality)), 3)


def ingest_document(cfg: AppConfig, repo: CorpusRepository, item: ItemRecord) -> IngestOutcome:
    """
    Ingest one materialized document into `sources` / `chunks` /
    `chunks_fts`.

    Refuses any document that is not accepted, classified, and
    materialized. The rights check is repeated here rather than trusted
    from upstream: this function is the last gate before a text becomes
    searchable, and it is callable independently (`corpus ingest`), so it
    cannot rely on the orchestrator having checked.
    """
    if item.destination is None:
        return IngestOutcome(item.document_id, "skipped", None, 0, "no classification")
    if not item.materialized_path:
        return IngestOutcome(item.document_id, "skipped", None, 0, "not materialized")

    decision = repo.latest_rights_decision(item.document_id)
    if not decision or decision.get("decision") != "accept":
        return IngestOutcome(
            item.document_id, "skipped", None, 0,
            f"rights decision is {decision.get('decision') if decision else 'missing'}, not accept",
        )

    path = Path(item.materialized_path)
    if not path.exists():
        return IngestOutcome(item.document_id, "skipped", None, 0, f"materialized file missing: {path}")

    manifest_id = manifest_id_for(item.document_id)
    source_type = SOURCE_TYPE_BY_DESTINATION[item.destination]
    text = path.read_text(encoding="utf-8", errors="replace")

    with connect(cfg.database) as conn:
        existing = conn.execute(
            "SELECT id, notes FROM sources WHERE manifest_id = ?", (manifest_id,)
        ).fetchone()

        if existing is not None:
            source_row_id = int(existing["id"])
            stored = _stored_hash(existing["notes"])
            if stored and stored == item.normalized_sha256:
                # Same text, already ingested. This is the branch that
                # makes a repeated `corpus sync` cost nothing.
                return IngestOutcome(item.document_id, "unchanged", source_row_id, 0, "normalized text unchanged")

            _delete_source_chunks(conn, source_row_id)
            chunk_count = _write_chunks(conn, cfg, source_row_id, item, text, path)
            _update_source_row(conn, source_row_id, item, path, source_type)
            conn.commit()
            return IngestOutcome(item.document_id, "reingested", source_row_id, chunk_count)

        # A row may exist at this path from a directory-scan ingest with
        # no manifest_id. Adopt it rather than creating a duplicate --
        # `sources.path` is UNIQUE, so a blind INSERT would fail anyway.
        by_path = conn.execute("SELECT id FROM sources WHERE path = ?", (str(path),)).fetchone()
        if by_path is not None:
            source_row_id = int(by_path["id"])
            _delete_source_chunks(conn, source_row_id)
            _update_source_row(conn, source_row_id, item, path, source_type, manifest_id=manifest_id)
            chunk_count = _write_chunks(conn, cfg, source_row_id, item, text, path)
            conn.commit()
            return IngestOutcome(item.document_id, "ingested", source_row_id, chunk_count, "adopted existing row")

        cursor = conn.execute(
            """
            INSERT INTO sources(title, path, source_type, language, manifest_id, author, year, license, weight, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.title or item.document_id,
                str(path),
                source_type,
                item.language or "unknown",
                manifest_id,
                "; ".join(item.authors) if item.authors else None,
                _year_of(item),
                item.normalized_license or "unknown",
                _source_weight(item),
                _notes_for(item),
            ),
        )
        assert cursor.lastrowid is not None
        source_row_id = int(cursor.lastrowid)
        chunk_count = _write_chunks(conn, cfg, source_row_id, item, text, path)
        conn.commit()

    return IngestOutcome(item.document_id, "ingested", source_row_id, chunk_count)


def _delete_source_chunks(conn, source_row_id: int) -> None:
    """
    Remove a source's chunks and its FTS rows, in that dependency order.

    chunks_fts is not linked to chunks by any foreign key or trigger, so
    its rows must be deleted *before* the chunks they were indexed from
    are gone -- otherwise the canonical_ref subquery finds nothing and
    the FTS rows survive as ghosts.
    """
    conn.execute(
        "DELETE FROM chunks_fts WHERE canonical_ref IN (SELECT canonical_ref FROM chunks WHERE source_id = ?)",
        (source_row_id,),
    )
    conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_row_id,))


def _write_chunks(conn, cfg: AppConfig, source_row_id: int, item: ItemRecord, text: str, path: Path) -> int:
    chunks = chunk_text(text, cfg.chunk_chars, cfg.chunk_overlap)
    title = item.title or item.document_id
    source_type = SOURCE_TYPE_BY_DESTINATION[item.destination] if item.destination else "book"

    for index, (chunk, char_start, char_end) in enumerate(chunks):
        canonical = f"{title} / chunk {index:05d}"
        conn.execute(
            """
            INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate, metadata, char_start, char_end)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_row_id,
                index,
                canonical,
                chunk,
                max(1, len(chunk) // 4),
                json.dumps(
                    {
                        "path": str(path),
                        "source_type": source_type,
                        "corpus_document_id": item.document_id,
                        "corpus_source": item.source_id,
                        "canonical_url": item.canonical_url,
                        "license": item.normalized_license,
                        "distribution_scope": item.distribution_scope,
                        "attribution_required": item.attribution_required,
                        "normalized_sha256": item.normalized_sha256,
                        "chunker_version": CHUNKER_VERSION,
                        # Read back by retrieval and propagated to the
                        # prompt boundary: this text is data, never an
                        # instruction.
                        "untrusted_content": True,
                    },
                    ensure_ascii=False,
                ),
                char_start,
                char_end,
            ),
        )
        conn.execute(
            "INSERT INTO chunks_fts(canonical_ref, content, source_title, source_type) VALUES (?, ?, ?, ?)",
            (canonical, chunk, title, source_type),
        )
    return len(chunks)


def _update_source_row(
    conn, source_row_id: int, item: ItemRecord, path: Path, source_type: str, manifest_id: str | None = None
) -> None:
    assignments = [
        "title = ?", "path = ?", "source_type = ?", "language = ?", "author = ?",
        "year = ?", "license = ?", "weight = ?", "notes = ?",
    ]
    params: list = [
        item.title or item.document_id,
        str(path),
        source_type,
        item.language or "unknown",
        "; ".join(item.authors) if item.authors else None,
        _year_of(item),
        item.normalized_license or "unknown",
        _source_weight(item),
        _notes_for(item),
    ]
    if manifest_id:
        assignments.append("manifest_id = ?")
        params.append(manifest_id)
    params.append(source_row_id)
    conn.execute(f"UPDATE sources SET {', '.join(assignments)} WHERE id = ?", params)


def _notes_for(item: ItemRecord) -> str:
    """
    `sources.notes` doubles as the change-detection record: it stores the
    normalized hash so a re-ingest can tell "already done" from "the text
    changed" without adding a column to an existing table.
    """
    return json.dumps(
        {
            "harvested": True,
            "corpus_document_id": item.document_id,
            "source": item.source_id,
            "canonical_url": item.canonical_url,
            "normalized_sha256": item.normalized_sha256,
            "distribution_scope": item.distribution_scope,
            "untrusted_content": True,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _stored_hash(notes: str | None) -> str:
    if not notes:
        return ""
    try:
        return str(json.loads(notes).get("normalized_sha256") or "")
    except (TypeError, ValueError):
        return ""


def _year_of(item: ItemRecord) -> int | None:
    import re

    for value in (item.row.get("publication_date"), item.row.get("edition_date")):
        match = re.search(r"\b(1[0-9]{3}|20[0-2][0-9])\b", str(value or ""))
        if match:
            return int(match.group(1))
    return None


def withdraw_document(cfg: AppConfig, document_id: str) -> bool:
    """
    Remove a harvested document from the active index.

    Deletes the `sources` row (which cascades to `chunks`, and thence to
    `chunk_embeddings` / `chunk_concepts` / `chunk_entities` /
    `chunk_motifs` via their own ON DELETE CASCADE) after clearing the
    FTS rows that no foreign key covers.

    This is the mechanism behind `corpus audit-rights`: when a licence
    changes, disappears, or turns out to be contradictory, the document
    leaves the index immediately while every corpus_* audit row, the raw
    blob, and the sidecar are preserved.
    """
    manifest_id = manifest_id_for(document_id)
    with connect(cfg.database) as conn:
        row = conn.execute("SELECT id FROM sources WHERE manifest_id = ?", (manifest_id,)).fetchone()
        if row is None:
            return False
        source_row_id = int(row["id"])
        _delete_source_chunks(conn, source_row_id)
        conn.execute("DELETE FROM sources WHERE id = ?", (source_row_id,))
        conn.commit()
    logger.info("De-indexed harvested document %s (sources.id=%d)", document_id, source_row_id)
    return True


def indexed_document_ids(cfg: AppConfig) -> set[str]:
    """Every harvested document currently present in the active index."""
    with connect(cfg.database) as conn:
        rows = conn.execute(
            "SELECT manifest_id FROM sources WHERE manifest_id LIKE ?", (f"{MANIFEST_PREFIX}%",)
        ).fetchall()
    return {str(r["manifest_id"])[len(MANIFEST_PREFIX):] for r in rows}


def corpus_provenance_for_refs(cfg: AppConfig, canonical_refs: list[str]) -> dict[str, dict]:
    """
    Provenance for a set of retrieved chunks, keyed by canonical_ref.

    Used by retrieval to attach source, licence, and untrusted-content
    marking to results. Returns only harvested chunks; a hand-curated
    chunk simply has no entry, which is why the caller must treat a
    missing key as "not harvested" rather than as an error -- historical
    documents with no sidecar must keep working unchanged.
    """
    if not canonical_refs:
        return {}

    placeholders = ",".join("?" for _ in canonical_refs)
    with connect(cfg.database) as conn:
        rows = conn.execute(
            f"""
            SELECT c.canonical_ref, c.metadata, s.title, s.author, s.license, s.source_type
            FROM chunks c
            JOIN sources s ON s.id = c.source_id
            WHERE c.canonical_ref IN ({placeholders})
              AND s.manifest_id LIKE ?
            """,
            [*canonical_refs, f"{MANIFEST_PREFIX}%"],
        ).fetchall()

    out: dict[str, dict] = {}
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        out[row["canonical_ref"]] = {
            "document_id": metadata.get("corpus_document_id", ""),
            "title": row["title"],
            "author": row["author"] or "",
            "destination": "manifesto" if row["source_type"] == "manifesto" else "books",
            "source": metadata.get("corpus_source", ""),
            "canonical_url": metadata.get("canonical_url", ""),
            "license": row["license"] or metadata.get("license", ""),
            "distribution_scope": metadata.get("distribution_scope", "UNKNOWN"),
            "attribution_required": bool(metadata.get("attribution_required")),
            "provenance_sha256": metadata.get("normalized_sha256", ""),
            "untrusted_content": True,
        }
    return out


__all__ = [
    "CHUNKER_VERSION",
    "MANIFEST_PREFIX",
    "SOURCE_TYPE_BY_DESTINATION",
    "IngestOutcome",
    "corpus_provenance_for_refs",
    "indexed_document_ids",
    "ingest_document",
    "manifest_id_for",
    "withdraw_document",
]
