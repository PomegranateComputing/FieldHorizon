from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.retrieval import hybrid_search_chunks
from fieldhorizon.storage_vectors import VectorStore


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
    with patch("fieldhorizon.retrieval.current_embedding_version", return_value="nomic-embed-text"):
        yield


def insert_chunk(cfg: AppConfig, path: str, content: str, source_weight: float = 1.0) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO sources(title, path, source_type, weight) VALUES ('t', ?, 'book', ?)",
            (path, source_weight),
        )
        source_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
            "VALUES (?, 0, ?, ?, 1)",
            (source_id, f"{path} / chunk 00000", content),
        )
        conn.commit()
        chunk_id = int(cur.lastrowid)

    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO chunks_fts(canonical_ref, content, source_title, source_type) VALUES (?, ?, 't', 'book')",
            (f"{path} / chunk 00000", content),
        )
        conn.commit()

    return chunk_id


def test_semantically_related_but_lexically_distant_chunk_is_retrievable(tmp_path):
    """
    Hybrid ranking sanity: a chunk that shares zero query terms (so FTS5
    MATCH alone would never surface it) but whose embedding is a near-exact
    match for the query embedding must still be retrievable -- this is the
    entire point of unioning vector search into candidate generation
    instead of relying on FTS5 alone.
    """
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    lexical_id = insert_chunk(cfg, "lexical", "the machine as idol of the modern age")
    semantic_id = insert_chunk(cfg, "semantic", "gardens and rivers and quiet orchards")

    store = VectorStore(cfg, "chunk", backend="blob")
    store.upsert(lexical_id, [0.0, 1.0], "nomic-embed-text")  # orthogonal to the query vector
    store.upsert(semantic_id, [1.0, 0.0], "nomic-embed-text")  # identical to the query vector

    with patch("fieldhorizon.retrieval.embed_text", return_value=[1.0, 0.0]):
        results = hybrid_search_chunks(cfg, "the machine as idol", limit=10)

    result_ids = {c.chunk_id for c in results}
    assert semantic_id in result_ids, "vector-only candidate must survive into the merged candidate set"


def test_explain_mode_reports_score_components(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    chunk_id = insert_chunk(cfg, "book1", "the machine as idol")

    store = VectorStore(cfg, "chunk", backend="blob")
    store.upsert(chunk_id, [1.0, 0.0], "nomic-embed-text")

    with patch("fieldhorizon.retrieval.embed_text", return_value=[1.0, 0.0]):
        results = hybrid_search_chunks(cfg, "the machine as idol", limit=5, explain=True)

    assert len(results) == 1
    components = results[0].components
    assert set(components) == {"vector_similarity", "bm25", "domain_prior", "source_weight", "severity"}
    assert abs(components["vector_similarity"].raw - 1.0) < 1e-6
    assert results[0].score == pytest.approx(sum(c.contribution for c in components.values()))


def test_explain_false_omits_components(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    chunk_id = insert_chunk(cfg, "book1", "the machine as idol")
    VectorStore(cfg, "chunk", backend="blob").upsert(chunk_id, [1.0, 0.0], "nomic-embed-text")

    with patch("fieldhorizon.retrieval.embed_text", return_value=[1.0, 0.0]):
        results = hybrid_search_chunks(cfg, "the machine as idol", limit=5, explain=False)

    assert results[0].components == {}


def test_source_weight_from_manifest_increases_score(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    low_weight_id = insert_chunk(cfg, "low", "the machine as idol", source_weight=0.1)
    high_weight_id = insert_chunk(cfg, "high", "the machine as idol", source_weight=3.0)

    store = VectorStore(cfg, "chunk", backend="blob")
    store.upsert(low_weight_id, [1.0, 0.0], "nomic-embed-text")
    store.upsert(high_weight_id, [1.0, 0.0], "nomic-embed-text")

    with patch("fieldhorizon.retrieval.embed_text", return_value=[1.0, 0.0]):
        results = hybrid_search_chunks(cfg, "the machine as idol", limit=10)

    scores = {c.chunk_id: c.score for c in results}
    assert scores[high_weight_id] > scores[low_weight_id]


def test_returns_empty_when_no_candidates_match_anything(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no model")):
        assert hybrid_search_chunks(cfg, "zzzznomatchzzzz", limit=5) == []


def test_weights_override_changes_score_without_touching_cfg(tmp_path):
    """Phase UI-4 item 3: RETRIEVAL lab adjusts weights per-request -- never mutates cfg.retrieval_weights itself."""
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    chunk_id = insert_chunk(cfg, "book1", "the machine as idol", source_weight=3.0)
    VectorStore(cfg, "chunk", backend="blob").upsert(chunk_id, [1.0, 0.0], "nomic-embed-text")

    from fieldhorizon.config import RetrievalWeights

    zero_source_weight = RetrievalWeights(vector_similarity=0.35, bm25=0.25, domain_prior=0.20, source_weight=0.0, severity=0.05)

    with patch("fieldhorizon.retrieval.embed_text", return_value=[1.0, 0.0]):
        default_results = hybrid_search_chunks(cfg, "the machine as idol", limit=5, explain=True)
        overridden_results = hybrid_search_chunks(cfg, "the machine as idol", limit=5, explain=True, weights=zero_source_weight)

    assert default_results[0].components["source_weight"].weight == cfg.retrieval_weights.source_weight
    assert overridden_results[0].components["source_weight"].weight == 0.0
    assert overridden_results[0].score < default_results[0].score
    assert cfg.retrieval_weights.source_weight == 0.15  # the override never mutated cfg itself


def test_config_hybrid_weights_defaults_are_used(tmp_path):
    cfg = dataclasses.replace(make_config(tmp_path))
    weights = cfg.retrieval_weights
    assert weights.vector_similarity == 0.35
    assert weights.bm25 == 0.25
    assert weights.domain_prior == 0.20
    assert weights.source_weight == 0.15
    assert weights.severity == 0.05
