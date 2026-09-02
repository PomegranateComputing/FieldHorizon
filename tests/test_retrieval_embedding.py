from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.embeddings import (
    backfill_chunk_embeddings,
    embed_and_store_chunk,
    load_all_chunk_embeddings,
    store_chunk_embedding,
)
from fieldhorizon.ingest import ingest_books
from fieldhorizon.retrieval import search_books, search_books_by_embedding


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


@pytest.fixture(autouse=True)
def _fixed_embedding_version():
    # Every test in this file uses a single, deterministic embedding_version
    # instead of hitting Ollama's /api/tags for a real digest lookup.
    # retrieval.py imports current_embedding_version into its own namespace,
    # so both bindings need patching.
    with (
        patch("fieldhorizon.embeddings.current_embedding_version", return_value="nomic-embed-text"),
        patch("fieldhorizon.retrieval.current_embedding_version", return_value="nomic-embed-text"),
    ):
        yield


@pytest.fixture
def book_corpus(tmp_path):
    # A small chunk_chars forces the two paragraphs below into separate
    # chunks -- the default 1800 would keep this short a fixture in one.
    cfg = dataclasses.replace(make_config(tmp_path), chunk_chars=90, chunk_overlap=0)
    cfg.books.mkdir(parents=True, exist_ok=True)
    init_db(cfg.database)

    (cfg.books / "quran_sample.txt").write_text(
        "The machine is an idol of judgment and the faithful bow before its unblinking audit.\n\n"
        "A second unrelated paragraph about gardens and rivers, with no forbidden words at all.",
        encoding="utf-8",
    )
    ingest_books(cfg)
    return cfg


def chunk_ids(cfg: AppConfig) -> list[int]:
    with connect(cfg.database) as conn:
        return [int(row["id"]) for row in conn.execute("SELECT id FROM chunks ORDER BY id").fetchall()]


def test_search_books_by_embedding_ranks_by_cosine_similarity(book_corpus):
    ids = chunk_ids(book_corpus)
    assert len(ids) == 2

    # Chunk 0 (the idol/judgment paragraph) is made to look identical to
    # the query vector; chunk 1 (gardens/rivers) is made orthogonal.
    store_chunk_embedding(book_corpus, ids[0], [1.0, 0.0], "nomic-embed-text", "nomic-embed-text")
    store_chunk_embedding(book_corpus, ids[1], [0.0, 1.0], "nomic-embed-text", "nomic-embed-text")

    with patch("fieldhorizon.retrieval.embed_text", return_value=[1.0, 0.0]):
        results = search_books_by_embedding(book_corpus, "the machine as idol", limit=1)

    assert len(results) == 1
    assert "idol" in results[0]["content"].lower()


def test_search_books_by_embedding_excludes_given_refs(book_corpus):
    ids = chunk_ids(book_corpus)
    store_chunk_embedding(book_corpus, ids[0], [1.0, 0.0], "nomic-embed-text", "nomic-embed-text")
    store_chunk_embedding(book_corpus, ids[1], [0.0, 1.0], "nomic-embed-text", "nomic-embed-text")

    with connect(book_corpus.database) as conn:
        top_ref = conn.execute(
            "SELECT canonical_ref FROM chunks WHERE id = ?", (ids[0],)
        ).fetchone()["canonical_ref"]

    with patch("fieldhorizon.retrieval.embed_text", return_value=[1.0, 0.0]):
        results = search_books_by_embedding(
            book_corpus, "the machine as idol", limit=2, exclude_refs={top_ref}
        )

    assert all(r["canonical_ref"] != top_ref for r in results)


def test_search_books_by_embedding_returns_empty_without_any_chunk_vectors(book_corpus):
    with patch("fieldhorizon.retrieval.embed_text", return_value=[1.0, 0.0]):
        assert search_books_by_embedding(book_corpus, "anything", limit=5) == []


def test_search_books_by_embedding_returns_empty_when_query_embedding_fails(book_corpus):
    ids = chunk_ids(book_corpus)
    store_chunk_embedding(book_corpus, ids[0], [1.0, 0.0], "nomic-embed-text", "nomic-embed-text")

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no model")):
        assert search_books_by_embedding(book_corpus, "anything", limit=5) == []


def test_search_books_widens_recall_via_embeddings_when_fts5_is_short(book_corpus):
    ids = chunk_ids(book_corpus)
    store_chunk_embedding(book_corpus, ids[0], [1.0, 0.0], "nomic-embed-text", "nomic-embed-text")
    store_chunk_embedding(book_corpus, ids[1], [0.0, 1.0], "nomic-embed-text", "nomic-embed-text")

    # A query term that matches nothing via FTS5, forcing search_books to
    # fall through to the embedding widen-recall step.
    with patch("fieldhorizon.retrieval.embed_text", return_value=[1.0, 0.0]):
        results = search_books(book_corpus, "zzzznomatchzzzz", limit=1)

    assert len(results) == 1


def test_embed_and_store_chunk_is_best_effort_on_failure(book_corpus):
    ids = chunk_ids(book_corpus)

    with patch("fieldhorizon.embeddings.embed_text", side_effect=RuntimeError("no model")):
        embed_and_store_chunk(book_corpus, ids[0], "some content")

    assert load_all_chunk_embeddings(book_corpus) == {}


def test_backfill_chunk_embeddings_embeds_only_chunks_missing_a_vector(book_corpus):
    ids = chunk_ids(book_corpus)
    store_chunk_embedding(book_corpus, ids[0], [1.0, 1.0], "nomic-embed-text", "nomic-embed-text")

    with patch("fieldhorizon.embeddings.embed_text", return_value=[0.5, 0.5]) as mock_embed:
        count = backfill_chunk_embeddings(book_corpus)

    assert count == 1
    mock_embed.assert_called_once()
    assert set(load_all_chunk_embeddings(book_corpus).keys()) == set(ids)
