from __future__ import annotations

from pathlib import Path

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.retrieval_plan import (
    STRATEGIES,
    STRATEGY_CANON_GENEALOGY,
    STRATEGY_CONTRADICTION,
    STRATEGY_DOMAIN_ROUTING,
    STRATEGY_ENTITY,
    STRATEGY_LEXICAL,
    STRATEGY_MOTIF,
    STRATEGY_SCHOOL,
    STRATEGY_TEMPORAL,
    STRATEGY_VECTOR,
    build_retrieval_plan,
)


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


def test_baseline_strategies_are_always_selected(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    plan = build_retrieval_plan(cfg, "a plain description of a garden and rivers")

    assert STRATEGY_LEXICAL in plan.strategies_selected
    assert STRATEGY_VECTOR in plan.strategies_selected
    assert STRATEGY_DOMAIN_ROUTING in plan.strategies_selected
    assert STRATEGY_CONTRADICTION not in plan.strategies_selected


def test_strategies_available_lists_the_full_vocabulary(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    plan = build_retrieval_plan(cfg, "a plain description of a garden and rivers")

    assert {s.name for s in plan.strategies_available} == set(STRATEGIES)
    assert all(s.backed for s in plan.strategies_available)
    temporal = next(s for s in plan.strategies_available if s.name == STRATEGY_TEMPORAL)
    assert "opt-in" in temporal.reason


def test_contradiction_activates_on_a_declared_ontology_opposition(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    # "logos" and "bureaucracy" are declared opposed in ontology.yaml
    # (meaning versus administration) -- no oppositional keyword present,
    # so this exercises the domain-pair path specifically.
    plan = build_retrieval_plan(cfg, "the truth of logos and the bureaucracy of compliance metrics")

    assert STRATEGY_CONTRADICTION in plan.strategies_selected
    assert plan.classification["oppositional"] is True
    assert any(pair[:2] == ("logos", "bureaucracy") for pair in plan.classification["opposing_domain_pairs"])


def test_contradiction_activates_on_an_oppositional_keyword_alone(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    plan = build_retrieval_plan(cfg, "cats versus dogs")

    assert STRATEGY_CONTRADICTION in plan.strategies_selected
    assert plan.classification["opposing_domain_pairs"] == []


def test_contradiction_does_not_activate_for_a_neutral_query(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    plan = build_retrieval_plan(cfg, "a plain description of a garden and rivers")

    assert STRATEGY_CONTRADICTION not in plan.strategies_selected
    assert plan.classification["oppositional"] is False


def _insert_chunk(cfg: AppConfig) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('S', '/tmp/s.txt', 'book')")
        source_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
            "VALUES (?, 0, 'ref-1', 'content', 1)",
            (source_id,),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_entity_activates_when_a_query_term_matches_a_known_entity(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    chunk_id = _insert_chunk(cfg)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO chunk_entities(chunk_id, entity, kind) VALUES (?, 'babel', 'place')", (chunk_id,)
        )
        conn.commit()

    plan = build_retrieval_plan(cfg, "the tower of babel and its builders")

    assert STRATEGY_ENTITY in plan.strategies_selected
    assert "babel" in plan.classification["entity_matches"]


def test_motif_activates_when_a_query_term_matches_a_known_motif(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    chunk_id = _insert_chunk(cfg)
    with connect(cfg.database) as conn:
        conn.execute("INSERT INTO chunk_motifs(chunk_id, motif) VALUES (?, 'flood')", (chunk_id,))
        conn.commit()

    plan = build_retrieval_plan(cfg, "a great flood consumed the land")

    assert STRATEGY_MOTIF in plan.strategies_selected
    assert "flood" in plan.classification["motif_matches"]


def _insert_active_canon_cycle(cfg: AppConfig, fragment: str) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, ?)",
            (fragment,),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_canon_genealogy_and_school_activate_on_a_matching_canon_fragment(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_active_canon_cycle(cfg, "the machine judges without mercy or memory")

    plan = build_retrieval_plan(cfg, "does the machine judge without mercy")

    assert STRATEGY_CANON_GENEALOGY in plan.strategies_selected
    assert STRATEGY_SCHOOL in plan.strategies_selected
    assert cycle_id in plan.classification["canon_matches"]


def test_canon_genealogy_does_not_activate_without_a_matching_fragment(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_active_canon_cycle(cfg, "an entirely unrelated fragment about gardens")

    plan = build_retrieval_plan(cfg, "the machine judges without mercy")

    assert STRATEGY_CANON_GENEALOGY not in plan.strategies_selected
    assert STRATEGY_SCHOOL not in plan.strategies_selected
