from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .db import connect
from .events import (
    ACTOR_CLI,
    AGGREGATE_INGEST,
    EVT_INGEST_COMPLETED,
    EVT_INGEST_FAILED,
    EVT_INGEST_SOURCE_COMPLETED,
    EVT_INGEST_STARTED,
    OperationEmitter,
)
from .manifest import SourceManifestEntry, load_manifest
from .provenance import record_extracted_from_edge, record_generated_axiom_edges

logger = logging.getLogger(__name__)

BOOK_EXTENSIONS = {".txt", ".md", ".markdown"}


def chunk_text(text: str, size: int, overlap: int) -> list[tuple[str, int, int]]:
    """
    Returns (content, char_start, char_end) triples -- exact offsets into
    `text` for the *stripped* content actually stored (Civilization Engine
    deep-provenance phase), not the raw size-based window. `.strip()`
    trims leading/trailing whitespace off each window, so char_start/
    char_end are adjusted inward from the window bounds to match.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    chunks: list[tuple[str, int, int]] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        window = text[start:end]
        chunk = window.strip()
        if chunk:
            leading = len(window) - len(window.lstrip())
            chunk_start = start + leading
            chunk_end = chunk_start + len(chunk)
            chunks.append((chunk, chunk_start, chunk_end))
        if end == len(text):
            break
        start = max(0, end - overlap)
    return chunks


def iter_book_files(path: Path) -> Iterable[Path]:
    if not path.exists():
        return []
    return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in BOOK_EXTENSIONS)


def infer_source_type(path: Path) -> str:
    low = path.name.lower()
    if "quran" in low or "coran" in low:
        return "sacred_quran"
    if "bible" in low or "kjv" in low:
        return "sacred_bible"
    if "red" in low and "flag" in low:
        return "red_flags"
    if "manifest" in low or "manifeste" in low:
        return "manifesto"
    return "book"

def upsert_source(cfg: AppConfig, title: str, source_type: str, path: str) -> int:
    with connect(cfg.database) as conn:
        row = conn.execute(
            """
            SELECT id
            FROM sources
            WHERE title = ? AND source_type = ? AND path = ?
            """,
            (title, source_type, path),
        ).fetchone()

        if row:
            return int(row["id"])

        cur = conn.execute(
            """
            INSERT INTO sources(title, source_type, path)
            VALUES (?, ?, ?)
            """,
            (title, source_type, path),
        )

        conn.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

def _delete_stale_fts_for_source(conn, source_id: int) -> None:
    """
    chunks_fts rows are not linked to `chunks` by any foreign key or
    trigger, so a re-ingested source's old FTS rows must be removed
    explicitly before the `chunks` rows they were indexed from disappear
    (review §5: without this, chunks_fts accumulates ghost entries on
    every re-ingest).
    """
    conn.execute(
        "DELETE FROM chunks_fts WHERE canonical_ref IN "
        "(SELECT canonical_ref FROM chunks WHERE source_id = ?)",
        (source_id,),
    )


def ingest_books(cfg: AppConfig) -> int:
    count = 0
    with connect(cfg.database) as conn:
        for path in iter_book_files(cfg.books):
            title = path.stem.replace("_", " ").replace("-", " ").strip()
            source_type = infer_source_type(path)
            text = path.read_text(encoding="utf-8", errors="replace")
            chunks = chunk_text(text, cfg.chunk_chars, cfg.chunk_overlap)

            old_source = conn.execute(
                "SELECT id FROM sources WHERE path = ?", (str(path),)
            ).fetchone()
            if old_source:
                _delete_stale_fts_for_source(conn, int(old_source["id"]))

            conn.execute("DELETE FROM sources WHERE path = ?", (str(path),))
            cur = conn.execute(
                "INSERT INTO sources(title, path, source_type) VALUES (?, ?, ?)",
                (title, str(path), source_type),
            )
            assert cur.lastrowid is not None
            source_id = int(cur.lastrowid)
            for i, (chunk, char_start, char_end) in enumerate(chunks):
                canonical = f"{title} / chunk {i:05d}"
                conn.execute(
                    """
                    INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate, metadata, char_start, char_end)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id, i, canonical, chunk, max(1, len(chunk) // 4),
                        json.dumps({"path": str(path)}), char_start, char_end,
                    ),
                )
                conn.execute(
                    "INSERT INTO chunks_fts(canonical_ref, content, source_title, source_type) VALUES (?, ?, ?, ?)",
                    (canonical, chunk, title, source_type),
                )
                count += 1
        conn.commit()
    return count


def stable_id(group: str, category: str, statement: str) -> str:
    digest = hashlib.sha256(f"{group}|{category}|{statement}".encode()).hexdigest()[:16]
    return f"{group}_{category}_{digest}".replace(" ", "_")


def normalize_entry(path: Path, item: dict) -> dict | None:
    statement = str(item.get("statement") or item.get("text") or item.get("axiom") or "").strip()
    if not statement:
        return None
    group = str(item.get("group") or path.parent.name or "synthetic")
    category = str(item.get("category") or path.stem)
    entry_id = str(item.get("id") or stable_id(group, category, statement))
    return {
        "id": entry_id,
        "group_name": group,
        "category": category,
        "tradition": str(item.get("tradition") or "synthetic"),
        "statement": statement,
        "gloss": str(item.get("gloss") or item.get("commentary") or ""),
        "targets": json.dumps(item.get("targets") or [], ensure_ascii=False),
        "tone": str(item.get("tone") or ""),
        "severity": float(item.get("severity") or 0.5),
        "mutation_potential": float(item.get("mutation_potential") or 0.5),
        "doctrinal_axes": json.dumps(item.get("doctrinal_axes") or item.get("axes") or [], ensure_ascii=False),
        "tags": json.dumps(item.get("tags") or [], ensure_ascii=False),
        "raw_json": json.dumps(item, ensure_ascii=False),
    }


def ingest_json_corpus(cfg: AppConfig) -> int:
    count = 0
    provenance_todo: list[tuple[str, dict | None, str]] = []
    files = sorted(p for p in cfg.json_corpus.rglob("*.json") if p.is_file()) if cfg.json_corpus.exists() else []
    with connect(cfg.database) as conn:
        for path in files:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Invalid JSON: %s: %s", path, exc)
                continue
            items = raw if isinstance(raw, list) else raw.get("entries", []) if isinstance(raw, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                entry = normalize_entry(path, item)
                if not entry:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO json_entries(
                        id, group_name, category, tradition, statement, gloss, targets, tone,
                        severity, mutation_potential, doctrinal_axes, tags, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(entry.values()),
                )
                # json_entries_fts has no unique constraint of its own (id
                # is not enforced there), so re-ingesting the same entry
                # would otherwise accumulate duplicate ghost rows forever.
                conn.execute("DELETE FROM json_entries_fts WHERE id = ?", (entry["id"],))
                conn.execute(
                    "INSERT INTO json_entries_fts(id, category, tradition, statement, gloss, tags) VALUES (?, ?, ?, ?, ?, ?)",
                    (entry["id"], entry["category"], entry["tradition"], entry["statement"], entry["gloss"], entry["tags"]),
                )
                count += 1
                provenance_todo.append((entry["id"], item.get("provenance"), entry["gloss"]))
        conn.commit()

    for axiom_id, provenance, gloss in provenance_todo:
        try:
            record_generated_axiom_edges(cfg, axiom_id, provenance)
            record_extracted_from_edge(cfg, axiom_id, gloss)
        except Exception as exc:
            # Best-effort, matching every other Phase C write hook: a
            # provenance edge failure must not turn a successful ingest
            # into an exception, or lose the json_entries row it describes.
            logger.warning("Provenance edge recording failed for json_entries id %s: %s", axiom_id, exc)
    return count


def ingest_manifestos(cfg: AppConfig) -> int:
    cfg.manifestos.mkdir(parents=True, exist_ok=True)

    total = 0

    for path in sorted(cfg.manifestos.glob("*.txt")):
        title = path.stem
        text = path.read_text(encoding="utf-8", errors="ignore")

        source_id = upsert_source(
            cfg,
            title=title,
            source_type="manifesto",
            path=str(path),
        )

        chunks = chunk_text(text, cfg.chunk_chars, cfg.chunk_overlap)

        with connect(cfg.database) as conn:
            _delete_stale_fts_for_source(conn, source_id)
            conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))

            for idx, (chunk, char_start, char_end) in enumerate(chunks):
                canonical_ref = f"manifesto / {title} / chunk {idx:05d}"

                conn.execute(
                    """
                    INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate, metadata, char_start, char_end)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        idx,
                        canonical_ref,
                        chunk,
                        max(1, len(chunk) // 4),
                        json.dumps({"path": str(path), "source_type": "manifesto"}),
                        char_start,
                        char_end,
                    ),
                )

                conn.execute(
                    """
                    INSERT INTO chunks_fts(canonical_ref, content, source_title, source_type)
                    VALUES (?, ?, ?, ?)
                    """,
                    (canonical_ref, chunk, title, "manifesto"),
                )

                total += 1

            conn.commit()

    return total


def backfill_chunk_offsets(cfg: AppConfig) -> tuple[int, int]:
    """
    For chunks ingested before char_start/char_end existed: re-read the
    chunk's own source file and search for its exact content within it.
    Best-effort (review's deep-provenance phase): a source file that has
    since changed, been re-encoded, or been deleted will miss, and misses
    are logged rather than raised -- this is a backfill, not a hard
    invariant the way canon_allowed's gates are.

    Returns (found, missed).
    """
    with connect(cfg.database) as conn:
        rows = conn.execute(
            """
            SELECT c.id AS chunk_id, c.content AS content, s.path AS source_path
            FROM chunks c
            JOIN sources s ON s.id = c.source_id
            WHERE c.char_start IS NULL OR c.char_end IS NULL
            ORDER BY c.id ASC
            """
        ).fetchall()

    found = 0
    missed = 0

    for row in rows:
        source_path = Path(row["source_path"])

        try:
            text = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning(
                "Chunk offset backfill: could not read source %s for chunk %d: %s",
                source_path, row["chunk_id"], exc,
            )
            missed += 1
            continue

        text = text.replace("\r\n", "\n").replace("\r", "\n")
        position = text.find(row["content"])

        if position == -1:
            logger.warning(
                "Chunk offset backfill: chunk %d's content not found verbatim in %s",
                row["chunk_id"], source_path,
            )
            missed += 1
            continue

        with connect(cfg.database) as conn:
            conn.execute(
                "UPDATE chunks SET char_start = ?, char_end = ? WHERE id = ?",
                (position, position + len(row["content"]), row["chunk_id"]),
            )
            conn.commit()

        found += 1

    return found, missed


@dataclass(frozen=True)
class ManifestIngestResult:
    manifest_id: str
    title: str
    status: str  # "ingested", "already_ingested", "adopted_existing", "missing_file"
    chunk_count: int
    source_id: int | None


def manifest_path(cfg: AppConfig) -> Path:
    return cfg.root / "data" / "sources.yaml"


def _adopt_existing_source(conn, cfg: AppConfig, entry: SourceManifestEntry, file_path: Path) -> ManifestIngestResult | None:
    """
    A source at this exact path may already have been ingested by the
    older directory-scan path (ingest_books/ingest_manifestos), before
    curation manifests existed -- those rows have manifest_id IS NULL. In
    that case ingest-manifest must not create a duplicate set of chunks
    for the same text; it adopts the existing source by stamping the
    manifest's id and provenance metadata onto it instead.
    """
    existing = conn.execute(
        "SELECT id FROM sources WHERE path = ? AND manifest_id IS NULL", (str(file_path),)
    ).fetchone()
    if existing is None:
        return None

    source_id = int(existing["id"])
    conn.execute(
        "UPDATE sources SET manifest_id = ?, author = ?, year = ?, license = ?, weight = ?, notes = ? WHERE id = ?",
        (entry.id, entry.author, entry.year, entry.license, entry.weight, entry.notes, source_id),
    )
    return ManifestIngestResult(entry.id, entry.title, "adopted_existing", 0, source_id)


def _ingest_manifest_entry(conn, cfg: AppConfig, entry: SourceManifestEntry) -> ManifestIngestResult:
    file_path = entry.resolve_path(cfg)
    if not file_path.exists():
        return ManifestIngestResult(entry.id, entry.title, "missing_file", 0, None)

    adopted = _adopt_existing_source(conn, cfg, entry, file_path)
    if adopted is not None:
        return adopted

    text = file_path.read_text(encoding="utf-8", errors="replace")
    chunks = chunk_text(text, cfg.chunk_chars, cfg.chunk_overlap)
    source_type = infer_source_type(file_path)

    cur = conn.execute(
        """
        INSERT INTO sources(title, path, source_type, manifest_id, author, year, license, weight, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.title, str(file_path), source_type, entry.id,
            entry.author, entry.year, entry.license, entry.weight, entry.notes,
        ),
    )
    assert cur.lastrowid is not None
    source_id = int(cur.lastrowid)

    for i, (chunk, char_start, char_end) in enumerate(chunks):
        canonical = f"{entry.title} / chunk {i:05d}"
        conn.execute(
            """
            INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate, metadata, char_start, char_end)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id, i, canonical, chunk, max(1, len(chunk) // 4),
                json.dumps({"path": str(file_path), "manifest_id": entry.id}), char_start, char_end,
            ),
        )
        conn.execute(
            "INSERT INTO chunks_fts(canonical_ref, content, source_title, source_type) VALUES (?, ?, ?, ?)",
            (canonical, chunk, entry.title, source_type),
        )

    return ManifestIngestResult(entry.id, entry.title, "ingested", len(chunks), source_id)


def ingest_from_manifest(
    cfg: AppConfig, actor: str = ACTOR_CLI, correlation_id: str | None = None
) -> list[ManifestIngestResult]:
    """
    Curation-as-code ingest (Civilization Engine corpus-scale phase):
    ingests exactly the files listed in data/sources.yaml, keyed by manifest
    id rather than path, so renaming a file on disk without editing the
    manifest does not trigger a re-ingest and does not silently ingest an
    unlisted file. Idempotent: a manifest id already present in `sources`
    is reported as already_ingested and left untouched -- re-running after
    adding new entries only ingests the new ones.

    Emits IngestStarted/IngestSourceCompleted (one per manifest entry)/
    IngestCompleted (or IngestFailed) via the same OperationEmitter cycle.py
    uses -- Phase UI-4 item 2's real per-source progress, watchable on
    GET /events/stream?run_id=<correlation_id>, not a fabricated percentage.
    correlation_id lets a caller (the /ingest/manifest route) pre-mint the
    run_id and start watching the stream before this synchronous call even
    returns, rather than only learning the id after the whole run finishes.
    """
    emitter = OperationEmitter(cfg, actor=actor, aggregate_type=AGGREGATE_INGEST, correlation_id=correlation_id)
    results: list[ManifestIngestResult] = []

    try:
        entries = load_manifest(manifest_path(cfg))
        emitter.emit(EVT_INGEST_STARTED, payload={"entry_count": len(entries)})

        for entry in entries:
            # One connection per entry, committed and closed *before*
            # emitter.emit() below opens its own (EventRepository.append's
            # own `with connect(...)`) -- emitting while this one was still
            # open as one big transaction across the whole manifest
            # deadlocked SQLite's single-writer lock against itself.
            with connect(cfg.database) as conn:
                existing = conn.execute(
                    "SELECT id FROM sources WHERE manifest_id = ?", (entry.id,)
                ).fetchone()
                if existing is not None:
                    result = ManifestIngestResult(entry.id, entry.title, "already_ingested", 0, int(existing["id"]))
                else:
                    result = _ingest_manifest_entry(conn, cfg, entry)
                conn.commit()
            results.append(result)
            emitter.emit(
                EVT_INGEST_SOURCE_COMPLETED,
                aggregate_id=result.source_id,
                payload={
                    "manifest_id": result.manifest_id,
                    "title": result.title,
                    "status": result.status,
                    "chunk_count": result.chunk_count,
                },
            )

        emitter.emit(
            EVT_INGEST_COMPLETED,
            payload={
                "entry_count": len(entries),
                "ingested_count": sum(1 for r in results if r.status == "ingested"),
            },
        )
    except Exception as exc:
        emitter.emit(EVT_INGEST_FAILED, payload={"error_type": type(exc).__name__, "error_message": str(exc)})
        raise

    return results

