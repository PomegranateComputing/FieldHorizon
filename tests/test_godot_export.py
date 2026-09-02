from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fieldhorizon.config import AppConfig
from fieldhorizon.contracts import (
    BeliefsResponse,
    ContradictionsResponse,
    DoctrinesResponse,
    FactionsResponse,
)
from fieldhorizon.db import connect, init_db
from fieldhorizon.engine import FieldHorizonEngine
from fieldhorizon.godot_export import _lineage_root_ids, build_contradictions, build_doctrines, export_godot


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


def insert_cycle(cfg: AppConfig, query: str, verdict: str, fragment: str, parent_ids=None) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, parent_cycle_ids) "
            "VALUES (?, 'm', 'p', 'r', 0, ?, 0.9, ?, ?)",
            (query, verdict, fragment, json.dumps(parent_ids) if parent_ids else None),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_lineage_root_ids_for_a_cycle_with_no_parents_is_itself(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "CANON", "f")
    assert _lineage_root_ids(cfg, cycle_id) == [cycle_id]


def test_lineage_root_ids_walks_back_through_parents(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    root_id = insert_cycle(cfg, "q1", "CANON", "root")
    middle_id = insert_cycle(cfg, "q2", "CANON", "middle", parent_ids=[root_id])
    leaf_id = insert_cycle(cfg, "q3", "CANON", "leaf", parent_ids=[middle_id])

    assert _lineage_root_ids(cfg, leaf_id) == [root_id]


def test_lineage_root_ids_handles_multiple_roots(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    root_a = insert_cycle(cfg, "q1", "CANON", "root a")
    root_b = insert_cycle(cfg, "q2", "CANON", "root b")
    child = insert_cycle(cfg, "q3", "CANON", "child", parent_ids=[root_a, root_b])

    assert _lineage_root_ids(cfg, child) == sorted([root_a, root_b])


def test_build_doctrines_reports_score_and_lineage(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "CANON", "a doctrine")
    engine = FieldHorizonEngine(cfg)

    result = build_doctrines(engine)
    assert isinstance(result, DoctrinesResponse)
    assert len(result.doctrines) == 1
    assert result.doctrines[0].fragment == "a doctrine"
    assert result.doctrines[0].lineage_root_ids == [cycle_id]


def test_build_contradictions_links_the_real_axiom_ids_and_pressure_type(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO json_entries(id, group_name, category, statement, severity, mutation_potential, raw_json) "
            "VALUES ('axiom_a', 'g', 'tawhid', 'unity is real', 0.8, 0.5, '{}')"
        )
        conn.execute(
            "INSERT INTO json_entries(id, group_name, category, statement, severity, mutation_potential, raw_json) "
            "VALUES ('axiom_b', 'g', 'multiplicity', 'fragmentation is real', 0.6, 0.5, '{}')"
        )
        conn.commit()
    engine = FieldHorizonEngine(cfg)

    result = build_contradictions(engine)
    assert isinstance(result, ContradictionsResponse)
    assert len(result.contradictions) == 1
    contradiction = result.contradictions[0]
    assert {contradiction.source_id, contradiction.target_id} == {"axiom_a", "axiom_b"}
    assert contradiction.pressure_type == "ontological_pressure"
    assert {contradiction.statement_a, contradiction.statement_b} == {"unity is real", "fragmentation is real"}


def test_export_godot_writes_all_five_files_matching_contracts(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    insert_cycle(cfg, "q", "CANON", "a doctrine")

    with patch("fieldhorizon.weather.load_cycle_embedding", return_value=None):
        written = export_godot(cfg, tmp_path / "exports")

    assert set(written) == {"factions.json", "doctrines.json", "contradictions.json", "weather.json", "beliefs.json"}

    factions_data = json.loads(written["factions.json"].read_text(encoding="utf-8"))
    FactionsResponse(**factions_data)  # raises if the written file doesn't validate

    doctrines_data = json.loads(written["doctrines.json"].read_text(encoding="utf-8"))
    doctrines = DoctrinesResponse(**doctrines_data)
    assert doctrines.doctrines[0].fragment == "a doctrine"

    contradictions_data = json.loads(written["contradictions.json"].read_text(encoding="utf-8"))
    ContradictionsResponse(**contradictions_data)

    beliefs_data = json.loads(written["beliefs.json"].read_text(encoding="utf-8"))
    BeliefsResponse(**beliefs_data)

    weather_data = json.loads(written["weather.json"].read_text(encoding="utf-8"))
    assert weather_data["schema_version"] == 1
    assert "canon" in weather_data and "surface" in weather_data
