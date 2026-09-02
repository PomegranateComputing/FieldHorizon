from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig, RetrievalPolicy
from fieldhorizon.db import connect, init_db
from fieldhorizon.retrieval_plan import plan_and_retrieve
from fieldhorizon.storage_vectors import VectorStore
from fieldhorizon.temporal import TemporalCanonRepository


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
        # This file tests strategy-surfacing logic, not the policy
        # constraints themselves (see test_retrieval_plan_policy.py) --
        # fixtures here have no real cycle_sources/SUPPORTS edges, so the
        # default min_direct_evidence_count=1 would otherwise filter every
        # canon candidate before the ancestry/school assertions ever run.
        retrieval_policy=RetrievalPolicy(max_per_source=3, min_direct_evidence_count=0, max_synthetic_evidence_ratio=1.0),
    )


@pytest.fixture(autouse=True)
def _fixed_embedding_version():
    with (
        patch("fieldhorizon.retrieval.current_embedding_version", return_value="nomic-embed-text"),
        patch("fieldhorizon.retrieval_plan.current_embedding_version", return_value="nomic-embed-text"),
    ):
        yield


def insert_chunk(cfg: AppConfig, path: str, content: str) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO sources(title, path, source_type) VALUES ('t', ?, 'book')", (path,)
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


def test_plan_and_retrieve_preserves_v2s_semantic_regression_guard(tmp_path):
    """A semantically-related, lexically-distant chunk must still surface -- no regression vs v2."""
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    lexical_id = insert_chunk(cfg, "lexical", "the machine as idol of the modern age")
    semantic_id = insert_chunk(cfg, "semantic", "gardens and rivers and quiet orchards")

    store = VectorStore(cfg, "chunk", backend="blob")
    store.upsert(lexical_id, [0.0, 1.0], "nomic-embed-text")
    store.upsert(semantic_id, [1.0, 0.0], "nomic-embed-text")

    with patch("fieldhorizon.retrieval.embed_text", return_value=[1.0, 0.0]):
        result = plan_and_retrieve(cfg, "the machine as idol", limit=10)

    chunk_ids = {c.chunk_id for c in result.chunk_candidates}
    assert semantic_id in chunk_ids


def test_entity_strategy_surfaces_a_chunk_fts_and_vector_never_found(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    unrelated_id = insert_chunk(cfg, "unrelated", "an entirely unrelated passage about weather patterns")
    with connect(cfg.database) as conn:
        conn.execute("INSERT INTO chunk_entities(chunk_id, entity, kind) VALUES (?, 'babel', 'place')", (unrelated_id,))
        conn.commit()

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        result = plan_and_retrieve(cfg, "the tower of babel", limit=10)

    assert "ENTITY" in result.plan.strategies_selected
    chunk_ids = {c.chunk_id for c in result.chunk_candidates}
    assert unrelated_id in chunk_ids
    matched = next(c for c in result.chunk_candidates if c.chunk_id == unrelated_id)
    assert "entity_match" in matched.components


def test_motif_strategy_surfaces_a_chunk_fts_and_vector_never_found(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    unrelated_id = insert_chunk(cfg, "unrelated", "an entirely unrelated passage about weather patterns")
    with connect(cfg.database) as conn:
        conn.execute("INSERT INTO chunk_motifs(chunk_id, motif) VALUES (?, 'flood')", (unrelated_id,))
        conn.commit()

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        result = plan_and_retrieve(cfg, "a great flood consumed the land", limit=10)

    assert "MOTIF" in result.plan.strategies_selected
    matched = next(c for c in result.chunk_candidates if c.chunk_id == unrelated_id)
    assert "motif_match" in matched.components


def test_contradiction_strategy_surfaces_a_chunk_from_the_opposing_domain(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    opposing_id = insert_chunk(cfg, "opposing", "an entirely unrelated passage about paperwork")
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO chunk_concepts(chunk_id, domain, confidence) VALUES (?, 'bureaucracy', 0.9)", (opposing_id,)
        )
        conn.commit()

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        result = plan_and_retrieve(cfg, "the truth of logos and the bureaucracy of compliance metrics", limit=10)

    assert "CONTRADICTION" in result.plan.strategies_selected
    matched = next(c for c in result.chunk_candidates if c.chunk_id == opposing_id)
    assert "contradiction_signal" in matched.components


def _insert_active_canon_cycle(cfg: AppConfig, fragment: str, parent_ids: list[int] | None = None) -> int:
    import json

    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, parent_cycle_ids) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, ?, ?)",
            (fragment, json.dumps(parent_ids) if parent_ids else None),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_canon_genealogy_strategy_surfaces_ancestors_with_graph_proximity(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    parent_id = _insert_active_canon_cycle(cfg, "an ancestor fragment about mercy")
    child_id = _insert_active_canon_cycle(cfg, "the machine judges without mercy or memory", parent_ids=[parent_id])

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        result = plan_and_retrieve(cfg, "does the machine judge without mercy", limit=10)

    assert "CANON_GENEALOGY" in result.plan.strategies_selected
    cycle_ids = {c.cycle_id for c in result.canon_candidates}
    assert child_id in cycle_ids
    assert parent_id in cycle_ids
    parent_candidate = next(c for c in result.canon_candidates if c.cycle_id == parent_id)
    assert "graph_proximity" in parent_candidate.components


def test_school_strategy_surfaces_sibling_cycles(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    matched_id = _insert_active_canon_cycle(cfg, "the machine judges without mercy or memory")
    sibling_id = _insert_active_canon_cycle(cfg, "an unrelated sibling fragment")

    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO schools(name, summary, run_at) VALUES ('S', 's', '2024-01-01 00:00:00')")
        school_id = int(cur.lastrowid)
        conn.execute("INSERT INTO school_members(school_id, cycle_id, distance) VALUES (?, ?, 0.1)", (school_id, matched_id))
        conn.execute("INSERT INTO school_members(school_id, cycle_id, distance) VALUES (?, ?, 0.1)", (school_id, sibling_id))
        conn.commit()

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        result = plan_and_retrieve(cfg, "does the machine judge without mercy", limit=10)

    assert "SCHOOL" in result.plan.strategies_selected
    cycle_ids = {c.cycle_id for c in result.canon_candidates}
    assert sibling_id in cycle_ids
    sibling_candidate = next(c for c in result.canon_candidates if c.cycle_id == sibling_id)
    assert "school_membership" in sibling_candidate.components


def test_temporal_filters_canon_candidates_by_as_of(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    matched_id = _insert_active_canon_cycle(cfg, "the machine judges without mercy or memory")
    TemporalCanonRepository(cfg).append(matched_id, "CANON", 0.9, valid_from="2024-03-01 00:00:00")

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        result_before = plan_and_retrieve(cfg, "does the machine judge without mercy", limit=10, as_of="2024-01-01 00:00:00")
        result_after = plan_and_retrieve(cfg, "does the machine judge without mercy", limit=10, as_of="2024-06-01 00:00:00")

    assert matched_id not in {c.cycle_id for c in result_before.canon_candidates}
    assert matched_id in {c.cycle_id for c in result_after.canon_candidates}


def test_diversity_contribution_appears_when_chunk_embeddings_exist(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    chunk_id = insert_chunk(cfg, "book1", "the machine as idol")

    store = VectorStore(cfg, "chunk", backend="blob")
    store.upsert(chunk_id, [1.0, 0.0], "nomic-embed-text")

    with patch("fieldhorizon.retrieval.embed_text", return_value=[1.0, 0.0]):
        result = plan_and_retrieve(cfg, "the machine as idol", limit=5)

    [candidate] = result.chunk_candidates
    assert "diversity_contribution" in candidate.components
    assert candidate.components["diversity_contribution"].raw == 1.0


def test_plan_and_retrieve_emits_planned_then_completed(tmp_path):
    from fieldhorizon.events import EventRepository

    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        plan_and_retrieve(cfg, "a plain description of a garden", limit=5)

    events = EventRepository(cfg).recent(limit=10)
    events.reverse()
    event_types = [e.event_type for e in events]
    assert event_types == ["RetrievalPlanned", "RetrievalCompleted"]
    assert len({e.correlation_id for e in events}) == 1
