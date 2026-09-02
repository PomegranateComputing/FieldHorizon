from __future__ import annotations

import json
from unittest.mock import patch

from fieldhorizon.config import AppConfig
from fieldhorizon.cycle import run_cycle
from fieldhorizon.db import connect, init_db
from fieldhorizon.evaluate import CycleEvaluation
from fieldhorizon.genealogy import load_canon_events


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


def insert_canon_cycle(cfg: AppConfig, query: str, fragment: str) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            """
            INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment)
            VALUES (?, 'test-model', 'prompt', 'response', 0, 'CANON', 0.9, ?)
            """,
            (query, fragment),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)


def canon_evaluation(verdict: str = "CANON") -> CycleEvaluation:
    return CycleEvaluation(
        symbolic_density=1.0,
        symbolic_density_method="keyword",
        doctrinal_enforcement=1.0,
        doctrinal_enforcement_method="keyword",
        length_score=1.0,
        structure_score=1.0,
        stuffing_penalty=0.0,
        generic_penalty=0.0,
        final_score=0.9,
        verdict=verdict,
        notes=[],
    )


def test_parent_cycle_ids_round_trips_through_a_real_cycle_write(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    ancestor_id = insert_canon_cycle(cfg, "an earlier query", "an earlier canon fragment")

    with (
        patch("fieldhorizon.cycle.generate_fragment", return_value="a generated fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", return_value=canon_evaluation()),
        patch("fieldhorizon.cycle.embed_and_store_fragment"),
        patch("fieldhorizon.cycle.record_canon_ngrams"),
        patch("fieldhorizon.cycle.record_cycle_weather"),
    ):
        cycle_id = run_cycle(cfg, "a new query", dry_run=False)

    with connect(cfg.database) as conn:
        row = conn.execute("SELECT parent_cycle_ids FROM cycles WHERE id = ?", (cycle_id,)).fetchone()

    assert row["parent_cycle_ids"] is not None
    assert json.loads(row["parent_cycle_ids"]) == [ancestor_id]


def test_promoted_event_is_recorded_when_a_cycle_reaches_canon(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.cycle.generate_fragment", return_value="a generated fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", return_value=canon_evaluation()),
        patch("fieldhorizon.cycle.embed_and_store_fragment"),
        patch("fieldhorizon.cycle.record_canon_ngrams"),
        patch("fieldhorizon.cycle.record_cycle_weather"),
    ):
        cycle_id = run_cycle(cfg, "a fresh query", dry_run=False)

    events = [e["event"] for e in load_canon_events(cfg, cycle_id)]
    assert events == ["PROMOTED"]


def test_no_promoted_event_when_a_cycle_does_not_reach_canon(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.cycle.generate_fragment", return_value="a generated fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", return_value=canon_evaluation(verdict="HERESY")),
        patch("fieldhorizon.cycle.embed_and_store_fragment") as mock_embed,
        patch("fieldhorizon.cycle.record_cycle_weather"),
    ):
        cycle_id = run_cycle(cfg, "a fresh query", dry_run=False)

    assert load_canon_events(cfg, cycle_id) == []
    mock_embed.assert_not_called()


def test_parent_cycle_ids_is_null_with_no_canon_ancestors(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.cycle.generate_fragment", return_value="a generated fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", return_value=canon_evaluation(verdict="HERESY")),
        patch("fieldhorizon.cycle.record_cycle_weather"),
    ):
        cycle_id = run_cycle(cfg, "a query with no canon yet", dry_run=False)

    with connect(cfg.database) as conn:
        row = conn.execute("SELECT parent_cycle_ids FROM cycles WHERE id = ?", (cycle_id,)).fetchone()

    assert row["parent_cycle_ids"] is None
