from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig, RetrievalPolicy
from fieldhorizon.db import connect, init_db
from fieldhorizon.retrieval_plan import plan_and_retrieve


def make_config(tmp_path: Path, policy: RetrievalPolicy) -> AppConfig:
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
        retrieval_policy=policy,
    )


@pytest.fixture(autouse=True)
def _fixed_embedding_version():
    with patch("fieldhorizon.retrieval.current_embedding_version", return_value="nomic-embed-text"):
        yield


def insert_chunk(cfg: AppConfig, path: str, content: str) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('t', ?, 'book')", (path,))
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


def test_max_per_source_excludes_and_records_why(tmp_path):
    cfg = make_config(tmp_path, RetrievalPolicy(max_per_source=1, min_direct_evidence_count=0, max_synthetic_evidence_ratio=1.0))
    init_db(cfg.database)

    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('same_source', '/p', 'book')")
        source_id = int(cur.lastrowid)
        for i in range(3):
            conn.execute(
                "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
                "VALUES (?, ?, ?, ?, 1)",
                (source_id, i, f"same_source / chunk {i:05d}", f"the machine as idol number {i}"),
            )
            conn.execute(
                "INSERT INTO chunks_fts(canonical_ref, content, source_title, source_type) VALUES (?, ?, 'same_source', 'book')",
                (f"same_source / chunk {i:05d}", f"the machine as idol number {i}"),
            )
        conn.commit()

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        result = plan_and_retrieve(cfg, "the machine as idol", limit=10)

    assert "max_per_source" in result.constraints_applied
    assert len(result.chunk_candidates) == 1
    assert len(result.excluded_chunk_candidates) == 2
    assert all("max_per_source" in c.exclusion_reason for c in result.excluded_chunk_candidates)


def test_max_per_source_does_not_exclude_when_under_the_limit(tmp_path):
    cfg = make_config(tmp_path, RetrievalPolicy(max_per_source=5, min_direct_evidence_count=0, max_synthetic_evidence_ratio=1.0))
    init_db(cfg.database)
    insert_chunk(cfg, "s1", "the machine as idol")

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        result = plan_and_retrieve(cfg, "the machine as idol", limit=10)

    assert result.excluded_chunk_candidates == []


def _insert_active_canon_cycle(cfg: AppConfig, fragment: str) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, ?)",
            (fragment,),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_min_direct_evidence_count_excludes_a_canon_candidate_with_no_supports_edges(tmp_path):
    cfg = make_config(tmp_path, RetrievalPolicy(max_per_source=3, min_direct_evidence_count=1, max_synthetic_evidence_ratio=1.0))
    init_db(cfg.database)
    cycle_id = _insert_active_canon_cycle(cfg, "the machine judges without mercy or memory")

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        result = plan_and_retrieve(cfg, "does the machine judge without mercy", limit=10)

    excluded_ids = {c.cycle_id for c in result.excluded_canon_candidates}
    assert cycle_id in excluded_ids
    excluded = next(c for c in result.excluded_canon_candidates if c.cycle_id == cycle_id)
    assert "min_direct_evidence_count" in excluded.exclusion_reason
    assert cycle_id not in {c.cycle_id for c in result.canon_candidates}


def test_min_direct_evidence_count_does_not_exclude_when_evidence_is_sufficient(tmp_path):
    cfg = make_config(tmp_path, RetrievalPolicy(max_per_source=3, min_direct_evidence_count=1, max_synthetic_evidence_ratio=1.0))
    init_db(cfg.database)
    cycle_id = _insert_active_canon_cycle(cfg, "the machine judges without mercy or memory")
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (?, 'json', 'axiom_1', 'c')",
            (cycle_id,),
        )
        conn.commit()

    from fieldhorizon.provenance import record_supports_edges

    record_supports_edges(cfg, cycle_id, [("json", "axiom_1")])

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        result = plan_and_retrieve(cfg, "does the machine judge without mercy", limit=10)

    assert cycle_id in {c.cycle_id for c in result.canon_candidates}
    assert cycle_id not in {c.cycle_id for c in result.excluded_canon_candidates}


def test_constraints_applied_always_lists_all_three(tmp_path):
    cfg = make_config(tmp_path, RetrievalPolicy(max_per_source=3, min_direct_evidence_count=0, max_synthetic_evidence_ratio=1.0))
    init_db(cfg.database)

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        result = plan_and_retrieve(cfg, "a plain description of a garden", limit=10)

    assert set(result.constraints_applied) == {
        "max_per_source", "min_direct_evidence_count", "max_synthetic_evidence_ratio",
    }
