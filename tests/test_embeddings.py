from __future__ import annotations

from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.embeddings import (
    backfill_canon_embeddings,
    blob_to_vector,
    cosine_similarity,
    embed_and_store_fragment,
    load_cycle_embedding,
    store_cycle_embedding,
    vector_to_blob,
)


def make_config(tmp_path) -> AppConfig:
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
    with patch("fieldhorizon.embeddings.current_embedding_version", return_value="nomic-embed-text"):
        yield


def insert_cycle(cfg: AppConfig, query: str, verdict: str, fragment: str, dry_run: int = 0) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            """
            INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (query, "test-model", "prompt", "response", dry_run, verdict, 0.9, fragment),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)


def test_vector_blob_roundtrip_preserves_values_within_float32_precision():
    vector = [0.1, -2.5, 3.333333, 0.0]
    restored = blob_to_vector(vector_to_blob(vector))
    assert len(restored) == len(vector)
    assert all(abs(a - b) < 1e-6 for a, b in zip(vector, restored, strict=True))


def test_cosine_similarity_identical_vectors_is_one():
    v = [1.0, 2.0, 3.0]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-6


def test_cosine_similarity_orthogonal_vectors_is_zero():
    assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-9


def test_cosine_similarity_handles_zero_vector():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_store_and_load_cycle_embedding_roundtrips(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "CANON", "a fragment")

    store_cycle_embedding(cfg, cycle_id, [1.0, 2.0, 3.0], "nomic-embed-text", "nomic-embed-text")

    loaded = load_cycle_embedding(cfg, cycle_id)
    assert loaded is not None
    assert all(abs(a - b) < 1e-6 for a, b in zip(loaded, [1.0, 2.0, 3.0], strict=True))


def test_load_cycle_embedding_returns_none_when_absent(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "CANON", "a fragment")
    assert load_cycle_embedding(cfg, cycle_id) is None


def test_embed_and_store_fragment_is_best_effort_on_failure(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "CANON", "a fragment")

    with patch("fieldhorizon.embeddings.embed_text", side_effect=RuntimeError("no model")):
        embed_and_store_fragment(cfg, cycle_id, "a fragment")

    assert load_cycle_embedding(cfg, cycle_id) is None


def test_load_cycle_embedding_ignores_a_stale_version_vector(tmp_path):
    # Standing rule: vectors from different embedding versions must never
    # be compared. A stored vector whose embedding_version doesn't match
    # the currently configured one must be treated identically to "not
    # embedded yet" -- not returned and silently compared anyway.
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "CANON", "a fragment")

    store_cycle_embedding(cfg, cycle_id, [1.0, 2.0, 3.0], "nomic-embed-text", "nomic-embed-text@oldsha123456")

    assert load_cycle_embedding(cfg, cycle_id) is None


def test_backfill_embeds_only_canon_cycles_missing_a_vector(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    canon_id = insert_cycle(cfg, "q1", "CANON", "canon fragment")
    heresy_id = insert_cycle(cfg, "q2", "HERESY", "heresy fragment")
    already_embedded_id = insert_cycle(cfg, "q3", "CANON", "already embedded")
    store_cycle_embedding(cfg, already_embedded_id, [1.0, 1.0], "nomic-embed-text", "nomic-embed-text")

    with patch("fieldhorizon.embeddings.embed_text", return_value=[0.5, 0.5]) as mock_embed:
        count = backfill_canon_embeddings(cfg)

    assert count == 1
    mock_embed.assert_called_once()
    assert load_cycle_embedding(cfg, canon_id) is not None
    assert load_cycle_embedding(cfg, heresy_id) is None
