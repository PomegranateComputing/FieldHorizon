from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fieldhorizon.cli import build_parser
from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.provenance import OBJECT_COUNCIL, OBJECT_CYCLE, REL_RETIRES, ProvenanceEdgeRepository
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
    )


def _write_config_yaml(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
paths:
  database: "{tmp_path / 'data.sqlite3'}"
  books: "{tmp_path / 'books'}"
  json_corpus: "{tmp_path / 'json_corpus'}"
  outputs: "{tmp_path / 'outputs'}"
  logs: "{tmp_path / 'logs'}"
  manifestos: "{tmp_path / 'manifestos'}"
""",
        encoding="utf-8",
    )
    return config_path


def _insert_cycle_with_temporal_state(cfg: AppConfig, created_at: str, verdict: str = "CANON") -> int:
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


def test_canon_cli_lists_current_canon_by_default(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    cycle_id = _insert_cycle_with_temporal_state(cfg, "2024-01-01 00:00:00")

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "canon"])
    args.func(args)

    out = capsys.readouterr().out
    assert str(cycle_id) in out


def test_canon_cli_as_of_timestamp_reflects_historical_state(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    early_id = _insert_cycle_with_temporal_state(cfg, "2024-01-01 00:00:00")
    _insert_cycle_with_temporal_state(cfg, "2024-03-01 00:00:00")

    parser = build_parser()
    args = parser.parse_args(
        ["--config", str(config_path), "canon", "--as-of-timestamp", "2024-02-01 00:00:00"]
    )
    args.func(args)

    out = capsys.readouterr().out
    assert str(early_id) in out
    assert "2024-03-01 00:00:00" not in out


def test_canon_cli_as_of_cycle_uses_that_cycles_created_at(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    early_id = _insert_cycle_with_temporal_state(cfg, "2024-01-01 00:00:00")
    later_id = _insert_cycle_with_temporal_state(cfg, "2024-03-01 00:00:00")

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "canon", "--as-of-cycle", str(later_id)])
    args.func(args)

    out = capsys.readouterr().out
    assert str(early_id) in out


def test_why_changed_cli_reports_no_transitions_for_an_untouched_cycle(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'f')"
        )
        cycle_id = int(cur.lastrowid)
        conn.commit()

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "why-changed", str(cycle_id)])
    args.func(args)

    out = capsys.readouterr().out
    assert "No verdict transitions recorded" in out


def test_why_changed_cli_reports_a_resolved_transition(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    cycle_id = _insert_cycle_with_temporal_state(cfg, "2024-01-01 00:00:00", verdict="HERESY")

    edge = ProvenanceEdgeRepository(cfg).append(OBJECT_COUNCIL, 1, OBJECT_CYCLE, cycle_id, REL_RETIRES)
    TemporalCanonRepository(cfg).append(
        cycle_id, "CANON", 0.9, valid_from="2024-02-01 00:00:00", source_edge_id=edge.id
    )

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "why-changed", str(cycle_id)])
    args.func(args)

    out = capsys.readouterr().out
    assert "CANON" in out
    assert REL_RETIRES in out


def test_weather_as_of_cli_reports_no_readings_when_none_exist(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "weather-as-of", "2024-01-01 00:00:00"])
    args.func(args)

    out = capsys.readouterr().out
    assert "No weather readings recorded" in out


def test_weather_as_of_cli_reports_a_historical_reading(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO weather_readings(recorded_at, scope, axis, value) VALUES ('2024-01-01 00:00:00', 'canon', 'tawhid', 0.5)"
        )
        conn.commit()

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "weather-as-of", "2024-02-01 00:00:00"])
    args.func(args)

    out = capsys.readouterr().out
    assert "tawhid" in out
    assert "0.500" in out


def test_school_membership_as_of_cli_reports_no_generation_when_none_exist(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "school-membership-as-of", "2024-01-01 00:00:00"])
    args.func(args)

    out = capsys.readouterr().out
    assert "No school generation was active" in out


def test_backfill_temporal_canon_cli_reports_states_written(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, created_at) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'f', '2024-01-01 00:00:00')"
        )
        conn.commit()

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "backfill-temporal-canon"])
    args.func(args)

    out = capsys.readouterr().out
    assert "Temporal canon states written" in out


def test_replay_level6_cli_requires_as_of(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'f')"
        )
        cycle_id = int(cur.lastrowid)
        conn.commit()

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "replay", str(cycle_id), "--level", "6"])

    try:
        args.func(args)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert exc.code == 1
    assert "--as-of is required" in capsys.readouterr().out


def test_replay_level6_cli_diffs_canon_states(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    early_id = _insert_cycle_with_temporal_state(cfg, "2024-01-01 00:00:00")
    _insert_cycle_with_temporal_state(cfg, "2024-03-01 00:00:00")

    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('the stored prompt', 'hermes3:8b', 'p', 'r', 0, 'CANON', 0.9, 'f')"
        )
        cycle_id = int(cur.lastrowid)
        conn.commit()

    parser = build_parser()
    args = parser.parse_args(
        ["--config", str(config_path), "replay", str(cycle_id), "--level", "6", "--as-of", "2024-02-01 00:00:00"]
    )
    with patch("fieldhorizon.replay.generate_fragment", return_value="a fresh fragment"):
        args.func(args)

    out = capsys.readouterr().out
    assert "L6 historical-canon replay" in out
    assert str(early_id) in out
