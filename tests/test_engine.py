from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.engine import FieldHorizonEngine
from fieldhorizon.lineage import LineageNotFoundError


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


def insert_cycle(cfg: AppConfig, query: str, verdict: str, fragment: str, score: float = 0.9, parent_ids=None) -> int:
    import json

    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, parent_cycle_ids) "
            "VALUES (?, 'm', 'p', 'r', 0, ?, ?, ?, ?)",
            (query, verdict, score, fragment, json.dumps(parent_ids) if parent_ids else None),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_engine_cycle_delegates_to_run_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.engine.run_cycle", return_value=42) as mock_run:
        cycle_id = engine.cycle("a query", model="m", dry_run=True, auto_rewrite=True)

    assert cycle_id == 42
    mock_run.assert_called_once_with(
        cfg, "a query", model="m", dry_run=True, auto_rewrite=True,
        critic_model=None, rewrite_model=None, json_domain=None, actor="cli", principal=None,
    )


def test_engine_canon_returns_active_canon_only_by_default(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    active_id = insert_cycle(cfg, "q1", "CANON", "active fragment")
    insert_cycle(cfg, "q2", "HERESY", "heresy fragment")

    entries = FieldHorizonEngine(cfg).canon()
    assert len(entries) == 1
    assert entries[0].cycle_id == active_id


def test_engine_canon_excludes_retired_unless_requested(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    retired_id = insert_cycle(cfg, "q1", "CANON", "retired fragment")
    with connect(cfg.database) as conn:
        conn.execute("UPDATE cycles SET retired_at = CURRENT_TIMESTAMP WHERE id = ?", (retired_id,))
        conn.commit()

    assert FieldHorizonEngine(cfg).canon() == []
    included = FieldHorizonEngine(cfg).canon(include_retired=True)
    assert len(included) == 1
    assert included[0].cycle_id == retired_id


def test_engine_canon_parses_parent_cycle_ids(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    parent_id = insert_cycle(cfg, "q1", "CANON", "parent")
    child_id = insert_cycle(cfg, "q2", "CANON", "child", parent_ids=[parent_id])

    entries = {e.cycle_id: e for e in FieldHorizonEngine(cfg).canon()}
    assert entries[child_id].parent_cycle_ids == [parent_id]
    assert entries[parent_id].parent_cycle_ids == []


def test_engine_lineage_raises_not_found_for_unknown_target(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    try:
        FieldHorizonEngine(cfg).lineage("nonexistent")
        raise AssertionError("expected LineageNotFoundError")
    except LineageNotFoundError:
        pass


def test_engine_lineage_returns_plain_text_not_a_rich_tree(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q1", "CANON", "a fragment")

    result = FieldHorizonEngine(cfg).lineage(str(cycle_id))
    assert isinstance(result.text, str)
    assert str(cycle_id) in result.text
    assert result.target_id == str(cycle_id)


def test_engine_provenance_dispatches_to_a_cycle_report(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q1", "CANON", "a fragment")

    report = FieldHorizonEngine(cfg).provenance(str(cycle_id))

    from fieldhorizon.provenance import OBJECT_CYCLE

    assert report.object_type == OBJECT_CYCLE
    assert report.object_id == str(cycle_id)


def test_engine_councils_lists_recorded_councils(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO councils(started_at, finished_at, examined, overturned, notes) "
            "VALUES ('2026-01-01', '2026-01-01', 3, 1, 'note')"
        )
        conn.commit()

    councils = FieldHorizonEngine(cfg).councils()
    assert len(councils) == 1
    assert councils[0].examined == 3
    assert councils[0].overturned == 1


def test_engine_schools_returns_empty_when_none_clustered(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert FieldHorizonEngine(cfg).schools() == []


def test_engine_schools_all_runs_returns_empty_when_none_clustered(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert FieldHorizonEngine(cfg).schools_all_runs() == []


def test_engine_school_members_returns_empty_for_an_unknown_school(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert FieldHorizonEngine(cfg).school_members(999) == []


def test_engine_school_distances_returns_empty_for_an_unknown_run(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert FieldHorizonEngine(cfg).school_distances("2020-01-01") == []


def test_engine_run_schools_clustering_returns_empty_below_the_minimum(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert FieldHorizonEngine(cfg).run_schools_clustering() == []


def test_engine_stats_counts_verdicts_and_corpus(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    insert_cycle(cfg, "q1", "CANON", "f1")
    insert_cycle(cfg, "q2", "HERESY", "f2")
    insert_cycle(cfg, "q3", "HERESY", "f3")

    stats = FieldHorizonEngine(cfg).stats()
    assert stats.cycles_total == 3
    assert stats.canon_count == 1
    assert stats.heresy_count == 2
    assert stats.sources_count == 0
    assert stats.dream_runs_count == 0
    assert stats.json_entries_count == 0
    assert stats.tagged_chunks_count == 0


def test_engine_stats_counts_json_entries_and_tagged_chunks(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO json_entries(id, group_name, category, statement, raw_json) VALUES ('e1', 'g', 'c', 's', '{}')"
        )
        conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('t', 'p', 'book')")
        conn.execute(
            "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
            "VALUES (1, 0, 'ref', 'content', 1)"
        )
        # Two chunks tagged across different tables, one untagged -- tagged_chunks_count
        # counts distinct chunks, not rows, so this must come back as 1, not 2.
        conn.execute("INSERT INTO chunk_concepts(chunk_id, domain, confidence) VALUES (1, 'tawhid', 0.8)")
        conn.execute("INSERT INTO chunk_motifs(chunk_id, motif) VALUES (1, 'a motif')")
        conn.commit()

    stats = FieldHorizonEngine(cfg).stats()
    assert stats.json_entries_count == 1
    assert stats.tagged_chunks_count == 1


def test_engine_fingerprint_returns_none_without_embedded_chunks(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('t', 'p', 'book')")
        conn.commit()
        source_id = conn.execute("SELECT id FROM sources").fetchone()["id"]

    with patch("fieldhorizon.fingerprint.current_embedding_version", return_value="nomic-embed-text"):
        assert FieldHorizonEngine(cfg).fingerprint(source_id) is None


def test_engine_weather_returns_canon_and_surface_keys(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    snapshot = FieldHorizonEngine(cfg).weather()
    assert snapshot.canon == {}
    assert snapshot.surface == {}
    assert isinstance(snapshot.surface_history, dict)
    assert isinstance(snapshot.canon_history, dict)


def test_engine_weather_canon_history_reads_back_recorded_readings(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO weather_readings(recorded_at, scope, axis, value) VALUES ('2020-01-01', 'canon', 'hierarchy', 0.4)"
        )
        conn.commit()

    snapshot = FieldHorizonEngine(cfg).weather()
    assert snapshot.canon_history["hierarchy"] == [0.4]


def test_engine_weather_by_school_is_empty_with_no_schools_computed(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert FieldHorizonEngine(cfg).weather_by_school() == []


def test_engine_weather_by_school_returns_one_reading_per_active_school(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "CANON", "a school member fragment")

    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO schools(name, summary, run_at) VALUES ('a school', 'a summary', '2020-01-01')"
        )
        school_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO school_members(school_id, cycle_id, distance) VALUES (?, ?, 0.1)", (school_id, cycle_id)
        )
        conn.commit()

    readings = FieldHorizonEngine(cfg).weather_by_school()
    assert len(readings) == 1
    assert readings[0].school_id == school_id
    assert readings[0].name == "a school"
    # No stored embedding for cycle_id -> school_weather_profile can't compute
    # anything real, and honestly reports an empty profile rather than guessing.
    assert readings[0].readings == {}


def _insert_cycle_with_temporal_state(cfg: AppConfig, created_at: str, verdict: str = "CANON") -> int:
    from fieldhorizon.temporal import TemporalCanonRepository

    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, created_at) "
            "VALUES ('q', 'm', 'p', 'r', 0, ?, 0.9, 'f', ?)",
            (verdict, created_at),
        )
        cycle_id = int(cur.lastrowid)
        conn.commit()

    TemporalCanonRepository(cfg).append(cycle_id, verdict, 0.9, valid_from=created_at)
    return cycle_id


def test_engine_canon_as_of_timestamp_returns_active_canon_entries(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    early_id = _insert_cycle_with_temporal_state(cfg, "2024-01-01 00:00:00")
    _insert_cycle_with_temporal_state(cfg, "2024-03-01 00:00:00")

    entries = FieldHorizonEngine(cfg).canon_as_of_timestamp("2024-02-01 00:00:00")
    assert [e.cycle_id for e in entries] == [early_id]


def test_engine_canon_as_of_cycle_uses_that_cycles_created_at(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    early_id = _insert_cycle_with_temporal_state(cfg, "2024-01-01 00:00:00")
    later_id = _insert_cycle_with_temporal_state(cfg, "2024-03-01 00:00:00")
    even_later_id = _insert_cycle_with_temporal_state(cfg, "2024-04-01 00:00:00")

    # later_id's own valid_from is exactly the cutoff (inclusive), so it
    # counts as active canon "as of itself"; even_later_id postdates it.
    entries = FieldHorizonEngine(cfg).canon_as_of_cycle(later_id)
    assert {e.cycle_id for e in entries} == {early_id, later_id}
    assert even_later_id not in {e.cycle_id for e in entries}


def test_engine_why_changed_returns_a_result_for_an_untouched_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle_with_temporal_state(cfg, "2024-01-01 00:00:00")

    result = FieldHorizonEngine(cfg).why_changed(cycle_id)
    assert result.cycle_id == cycle_id
    assert len(result.transitions) == 1


def test_engine_school_membership_as_of_returns_the_active_generation(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "CANON", "f")
    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO schools(name, summary, run_at) VALUES ('S', 's', '2024-01-01 00:00:00')")
        school_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO school_members(school_id, cycle_id, distance) VALUES (?, ?, 0.1)", (school_id, cycle_id)
        )
        conn.commit()

    schools = FieldHorizonEngine(cfg).school_membership_as_of("2024-02-01 00:00:00")
    assert [s.id for s in schools] == [school_id]


def test_engine_weather_as_of_reads_a_historical_reading(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO weather_readings(recorded_at, scope, axis, value) VALUES ('2024-01-01 00:00:00', 'canon', 'tawhid', 0.5)"
        )
        conn.commit()

    assert FieldHorizonEngine(cfg).weather_as_of("2024-02-01 00:00:00") == {"tawhid": 0.5}


def test_engine_retrieve_planned_returns_a_retrieval_plan_result(tmp_path):
    from unittest.mock import patch

    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        result = FieldHorizonEngine(cfg).retrieve_planned("a plain description of a garden", limit=5)

    assert result.plan.query == "a plain description of a garden"
    assert result.chunk_candidates == []
    assert result.canon_candidates == []
