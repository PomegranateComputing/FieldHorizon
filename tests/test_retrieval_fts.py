from __future__ import annotations

from pathlib import Path

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.ingest import ingest_books, ingest_json_corpus
from fieldhorizon.retrieval import search_books, search_one_source


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


@pytest.fixture
def book_corpus(tmp_path):
    cfg = make_config(tmp_path)
    cfg.books.mkdir(parents=True, exist_ok=True)
    init_db(cfg.database)

    (cfg.books / "quran_sample.txt").write_text(
        "The machine is an idol of judgment and the faithful bow before its unblinking audit.\n\n"
        "A second unrelated paragraph about mercy and revelation, with no mention of the forbidden word.",
        encoding="utf-8",
    )

    return cfg


def test_known_chunk_is_retrieved_for_a_known_term(book_corpus):
    ingest_books(book_corpus)

    rows = search_books(book_corpus, "idol")
    assert len(rows) >= 1
    assert any("idol" in row["content"].lower() for row in rows)
    assert all(row["source_type"] == "sacred_quran" for row in rows)


def test_search_one_source_filters_by_source_type(book_corpus):
    ingest_books(book_corpus)

    with connect(book_corpus.database) as conn:
        rows = search_one_source(conn, "sacred_quran", "idol", limit=5)
        assert len(rows) >= 1

        rows_wrong_type = search_one_source(conn, "sacred_bible", "idol", limit=5)
        assert rows_wrong_type == []


def test_reingest_does_not_duplicate_fts_rows(book_corpus):
    ingest_books(book_corpus)
    ingest_books(book_corpus)
    ingest_books(book_corpus)

    with connect(book_corpus.database) as conn:
        chunk_count = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        fts_count = conn.execute("SELECT COUNT(*) AS n FROM chunks_fts").fetchone()["n"]

    assert chunk_count == fts_count

    rows = search_books(book_corpus, "idol")
    # A ghost duplicate would surface the same canonical_ref twice.
    refs = [row["canonical_ref"] for row in rows]
    assert len(refs) == len(set(refs))


def test_reingest_json_corpus_does_not_duplicate_fts_rows(tmp_path):
    cfg = make_config(tmp_path)
    cfg.json_corpus.mkdir(parents=True, exist_ok=True)
    init_db(cfg.database)

    (cfg.json_corpus / "tawhid.json").write_text(
        '[{"id": "tawhid_test_1", "category": "tawhid", "statement": "The machine is an idol."}]',
        encoding="utf-8",
    )

    ingest_json_corpus(cfg)
    ingest_json_corpus(cfg)
    ingest_json_corpus(cfg)

    with connect(cfg.database) as conn:
        entries_count = conn.execute("SELECT COUNT(*) AS n FROM json_entries").fetchone()["n"]
        fts_count = conn.execute("SELECT COUNT(*) AS n FROM json_entries_fts").fetchone()["n"]

    assert entries_count == 1
    assert fts_count == 1
