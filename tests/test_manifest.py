from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.events import EventRepository
from fieldhorizon.ingest import ingest_from_manifest, manifest_path
from fieldhorizon.manifest import ManifestError, load_manifest


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        root=tmp_path,
        database=tmp_path / "data.sqlite3",
        books=tmp_path / "books",
        json_corpus=tmp_path / "json_corpus",
        outputs=tmp_path / "outputs",
        logs=tmp_path / "logs",
        ollama_base_url="http://localhost:11434",
        default_model="hermes3:8b",
        temperature=1.25,
        top_p=0.95,
        repeat_penalty=1.08,
        num_ctx=8192,
        book_fragments=6,
        json_entries=8,
        chunk_chars=1800,
        chunk_overlap=250,
        tone="dark",
        mode="canonical_synthesis",
        manifestos=tmp_path / "manifestos",
        embedding_model="nomic-embed-text",
    )


def write_manifest(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "data" / "sources.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_load_manifest_missing_file_raises(tmp_path):
    with pytest.raises(ManifestError, match="Missing manifest file"):
        load_manifest(tmp_path / "nope.yaml")


def test_load_manifest_top_level_must_be_a_mapping(tmp_path):
    path = write_manifest(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ManifestError, match="expected a YAML mapping"):
        load_manifest(path)


def test_load_manifest_sources_must_be_a_list(tmp_path):
    path = write_manifest(tmp_path, "sources: not-a-list\n")
    with pytest.raises(ManifestError, match="'sources' must be a list"):
        load_manifest(path)


def test_load_manifest_rejects_missing_required_field(tmp_path):
    path = write_manifest(
        tmp_path,
        "sources:\n  - title: 'Missing id'\n    path: data/books/x.txt\n",
    )
    with pytest.raises(ManifestError, match=r"sources\[0\]\.id"):
        load_manifest(path)


def test_load_manifest_rejects_non_positive_weight(tmp_path):
    path = write_manifest(
        tmp_path,
        "sources:\n  - id: x\n    title: X\n    path: data/books/x.txt\n    weight: 0\n",
    )
    with pytest.raises(ManifestError, match="weight must be positive"):
        load_manifest(path)


def test_load_manifest_rejects_duplicate_ids(tmp_path):
    path = write_manifest(
        tmp_path,
        "sources:\n"
        "  - id: dup\n    title: A\n    path: data/books/a.txt\n"
        "  - id: dup\n    title: B\n    path: data/books/b.txt\n",
    )
    with pytest.raises(ManifestError, match="duplicate source id"):
        load_manifest(path)


def test_load_manifest_applies_defaults(tmp_path):
    path = write_manifest(
        tmp_path,
        "sources:\n  - id: minimal\n    title: Minimal\n    path: data/books/x.txt\n",
    )
    entries = load_manifest(path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.author is None
    assert entry.year is None
    assert entry.language == "unknown"
    assert entry.license == "unknown"
    assert entry.domain_hints == ()
    assert entry.weight == 1.0
    assert entry.notes == ""


def test_ingest_from_manifest_ingests_a_listed_file(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    (cfg.root / "data" / "books").mkdir(parents=True, exist_ok=True)
    (cfg.root / "data" / "books" / "sample.txt").write_text("A short sample text.", encoding="utf-8")
    write_manifest(
        tmp_path,
        "sources:\n"
        "  - id: sample\n    title: Sample\n    path: data/books/sample.txt\n"
        "    author: Someone\n    year: 2020\n    license: public-domain\n    weight: 2.0\n",
    )

    results = ingest_from_manifest(cfg)
    assert len(results) == 1
    assert results[0].status == "ingested"
    assert results[0].chunk_count == 1

    with connect(cfg.database) as conn:
        row = conn.execute(
            "SELECT manifest_id, author, year, license, weight FROM sources WHERE id = ?",
            (results[0].source_id,),
        ).fetchone()
    assert row["manifest_id"] == "sample"
    assert row["author"] == "Someone"
    assert row["year"] == 2020
    assert row["license"] == "public-domain"
    assert row["weight"] == 2.0


def test_ingest_from_manifest_is_idempotent_by_manifest_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    (cfg.root / "data" / "books").mkdir(parents=True, exist_ok=True)
    (cfg.root / "data" / "books" / "sample.txt").write_text("A short sample text.", encoding="utf-8")
    write_manifest(
        tmp_path,
        "sources:\n  - id: sample\n    title: Sample\n    path: data/books/sample.txt\n",
    )

    first = ingest_from_manifest(cfg)
    second = ingest_from_manifest(cfg)

    assert first[0].status == "ingested"
    assert second[0].status == "already_ingested"
    with connect(cfg.database) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"] == first[0].chunk_count


def test_ingest_from_manifest_reports_missing_file(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    write_manifest(
        tmp_path,
        "sources:\n  - id: ghost\n    title: Ghost\n    path: data/books/ghost.txt\n",
    )

    results = ingest_from_manifest(cfg)
    assert results[0].status == "missing_file"
    assert results[0].source_id is None


def test_ingest_from_manifest_adopts_a_source_ingested_by_the_old_directory_scan_path(tmp_path):
    """
    A source at the same path may already exist with manifest_id IS NULL
    (ingested via the older ingest_books/ingest_manifestos directory-scan
    path, before curation manifests existed). ingest-manifest must adopt
    that row -- stamping manifest metadata onto it -- rather than creating
    a second, duplicate set of chunks for the same text.
    """
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    (cfg.root / "data" / "books").mkdir(parents=True, exist_ok=True)
    book_path = cfg.root / "data" / "books" / "sample.txt"
    book_path.write_text("Some pre-existing text.", encoding="utf-8")

    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO sources(title, path, source_type) VALUES (?, ?, ?)",
            ("Sample", str(book_path), "book"),
        )
        conn.commit()
        pre_existing_source_id = conn.execute("SELECT id FROM sources").fetchone()["id"]
        conn.execute(
            "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
            "VALUES (?, 0, 'ref', ?, 1)",
            (pre_existing_source_id, "Some pre-existing text."),
        )
        conn.commit()

    write_manifest(
        tmp_path,
        "sources:\n  - id: sample\n    title: Sample\n    path: data/books/sample.txt\n    weight: 1.5\n",
    )

    results = ingest_from_manifest(cfg)
    assert results[0].status == "adopted_existing"
    assert results[0].source_id == pre_existing_source_id

    with connect(cfg.database) as conn:
        chunk_count = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        row = conn.execute("SELECT manifest_id, weight FROM sources WHERE id = ?", (pre_existing_source_id,)).fetchone()

    assert chunk_count == 1  # no duplicate chunks created
    assert row["manifest_id"] == "sample"
    assert row["weight"] == 1.5


def test_manifest_path_resolves_relative_to_config_root(tmp_path):
    cfg = dataclasses.replace(make_config(tmp_path))
    assert manifest_path(cfg) == tmp_path / "data" / "sources.yaml"


def test_ingest_from_manifest_emits_real_progress_events(tmp_path):
    """
    Phase UI-4 item 2: a client watching GET /events/stream?run_id= during
    ingestion needs a real per-source event, not just a start/end pair.
    """
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    (cfg.root / "data" / "books").mkdir(parents=True, exist_ok=True)
    (cfg.root / "data" / "books" / "a.txt").write_text("First sample text.", encoding="utf-8")
    (cfg.root / "data" / "books" / "b.txt").write_text("Second sample text.", encoding="utf-8")
    write_manifest(
        tmp_path,
        "sources:\n"
        "  - id: a\n    title: A\n    path: data/books/a.txt\n"
        "  - id: b\n    title: B\n    path: data/books/b.txt\n",
    )

    ingest_from_manifest(cfg, correlation_id="test-run-id")

    events = EventRepository(cfg).by_run("test-run-id")
    event_types = [e.event_type for e in events]
    assert event_types == [
        "IngestStarted",
        "IngestSourceCompleted",
        "IngestSourceCompleted",
        "IngestCompleted",
    ]
    assert events[0].payload == {"entry_count": 2}
    assert events[1].payload["manifest_id"] == "a"
    assert events[1].payload["status"] == "ingested"
    assert events[3].payload == {"entry_count": 2, "ingested_count": 2}


def test_ingest_from_manifest_emits_failure_event_and_reraises(tmp_path):
    """A bad manifest (or any other failure) still leaves a real IngestFailed
    event behind for a client watching the stream -- not a silent raise with
    no trail at all."""
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    # No data/books directory or manifest file at all -- load_manifest itself raises.
    with pytest.raises(ManifestError):
        ingest_from_manifest(cfg, correlation_id="failed-run")

    events = EventRepository(cfg).by_run("failed-run")
    assert [e.event_type for e in events] == ["IngestFailed"]
    assert events[0].payload["error_type"] == "ManifestError"
