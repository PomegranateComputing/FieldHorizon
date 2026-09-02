from __future__ import annotations

from unittest.mock import patch

import pytest

from fieldhorizon.canon import load_canon_fragments, load_motif_counts, record_canon_ngrams
from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.embeddings import store_cycle_embedding


@pytest.fixture(autouse=True)
def _fixed_embedding_version():
    # Every test in this file uses a single, deterministic embedding_version
    # instead of hitting Ollama's /api/tags for a real digest lookup.
    with patch("fieldhorizon.embeddings.current_embedding_version", return_value="nomic-embed-text"):
        yield


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


def insert_cycle(cfg: AppConfig, query: str, fragment: str) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            """
            INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment)
            VALUES (?, ?, ?, ?, 0, 'CANON', 0.9, ?)
            """,
            (query, "test-model", "prompt", "response", fragment),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)


def test_mmr_prefers_a_diverse_fragment_over_a_near_duplicate(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    id_a = insert_cycle(cfg, "q", "fragment A")
    id_b = insert_cycle(cfg, "q", "fragment B")
    id_c = insert_cycle(cfg, "q", "fragment C")

    query_vector = [1.0, 0.0]
    store_cycle_embedding(cfg, id_a, [1.0, 0.2], "nomic-embed-text", "nomic-embed-text")   # highest relevance
    store_cycle_embedding(cfg, id_b, [1.0, 0.25], "nomic-embed-text", "nomic-embed-text")  # near-duplicate of A
    store_cycle_embedding(cfg, id_c, [1.0, -3.0], "nomic-embed-text", "nomic-embed-text")  # lower relevance, very different from A

    with patch("fieldhorizon.canon.embed_text", return_value=query_vector):
        fragments = load_canon_fragments(cfg, query="test query", limit=2, pool_size=10)

    refs = {f["ref"] for f in fragments}
    assert refs == {f"canon / cycle_{id_a}", f"canon / cycle_{id_c}"}


def test_mmr_falls_back_to_recency_when_no_embeddings_exist(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    insert_cycle(cfg, "q1", "oldest")
    insert_cycle(cfg, "q2", "middle")
    id_newest = insert_cycle(cfg, "q3", "newest")

    fragments = load_canon_fragments(cfg, query="anything", limit=1, pool_size=10)

    assert fragments == [
        {
            "cycle_id": id_newest,
            "ref": f"canon / cycle_{id_newest}",
            "content": "newest",
            "path": f"cycles.id={id_newest}",
        }
    ]


def test_mmr_falls_back_to_recency_when_query_embedding_fails(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    id_a = insert_cycle(cfg, "q", "fragment A")
    id_b = insert_cycle(cfg, "q", "fragment B")
    store_cycle_embedding(cfg, id_a, [1.0, 0.0], "nomic-embed-text", "nomic-embed-text")
    store_cycle_embedding(cfg, id_b, [0.0, 1.0], "nomic-embed-text", "nomic-embed-text")

    with patch("fieldhorizon.canon.embed_text", side_effect=RuntimeError("no embedding model")):
        fragments = load_canon_fragments(cfg, query="anything", limit=1, pool_size=10)

    assert fragments[0]["ref"] == f"canon / cycle_{id_b}"


def test_retired_canon_is_excluded_from_load_canon_fragments(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    active_id = insert_cycle(cfg, "q1", "active fragment")
    retired_id = insert_cycle(cfg, "q2", "retired fragment")

    with connect(cfg.database) as conn:
        conn.execute(
            "UPDATE cycles SET retired_at = CURRENT_TIMESTAMP, retirement_reason = 'test' WHERE id = ?",
            (retired_id,),
        )
        conn.commit()

    fragments = load_canon_fragments(cfg, query="anything", limit=10, pool_size=10)

    refs = {f["ref"] for f in fragments}
    assert refs == {f"canon / cycle_{active_id}"}


def test_record_and_load_motif_counts_across_window(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    id_1 = insert_cycle(cfg, "q1", "the machine judges the faithful without mercy")
    id_2 = insert_cycle(cfg, "q2", "the machine judges the crowd without mercy")

    record_canon_ngrams(cfg, id_1, "the machine judges the faithful without mercy")
    record_canon_ngrams(cfg, id_2, "the machine judges the crowd without mercy")

    counts = load_motif_counts(cfg, window=20)

    # "the machine judges" appears in both fragments -> frequency 2.
    assert counts["the machine judges"] == 2
    # A 3-gram unique to one fragment stays at frequency 1.
    assert counts["judges the faithful"] == 1


def test_load_motif_counts_respects_window(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    id_old = insert_cycle(cfg, "q1", "an overused phrase repeats here")
    record_canon_ngrams(cfg, id_old, "an overused phrase repeats here")

    id_new = insert_cycle(cfg, "q2", "a fresh fragment with different words")
    record_canon_ngrams(cfg, id_new, "a fresh fragment with different words")

    counts = load_motif_counts(cfg, window=1)

    assert "an overused phrase" not in counts
    assert "a fresh fragment" in counts
