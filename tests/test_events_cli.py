from __future__ import annotations

from pathlib import Path

from fieldhorizon.cli import build_parser
from fieldhorizon.config import AppConfig
from fieldhorizon.db import init_db
from fieldhorizon.events import EventRepository, new_correlation_id


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


def test_events_list_reports_no_events_on_a_fresh_db(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "events", "list"])
    args.func(args)

    out = capsys.readouterr().out
    assert "No events recorded yet" in out


def test_events_list_shows_recorded_events(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    repo = EventRepository(cfg)
    repo.append(event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id=new_correlation_id())

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "events", "list"])
    args.func(args)

    out = capsys.readouterr().out
    assert "CycleStarted" in out
    assert "cli" in out


def test_events_show_prints_full_detail(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    repo = EventRepository(cfg)
    event = repo.append(
        event_type="CycleCompleted", actor="cli", aggregate_type="cycle",
        correlation_id=new_correlation_id(), aggregate_id=42, payload={"verdict": "CANON"},
    )

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "events", "show", event.event_id])
    args.func(args)

    out = capsys.readouterr().out
    assert "CycleCompleted" in out
    assert event.correlation_id in out
    assert "verdict" in out


def test_events_show_reports_unknown_event_id(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "events", "show", "not-a-real-id"])

    try:
        args.func(args)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert exc.code == 1

    out = capsys.readouterr().out
    assert "No event found" in out


def test_events_trace_returns_all_events_for_a_correlation_id(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    repo = EventRepository(cfg)
    correlation_id = new_correlation_id()
    repo.append(event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id=correlation_id)
    repo.append(event_type="CycleCompleted", actor="cli", aggregate_type="cycle", correlation_id=correlation_id)
    repo.append(event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id=new_correlation_id())

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "events", "trace", correlation_id])
    args.func(args)

    out = capsys.readouterr().out
    assert "CycleStarted" in out
    assert "CycleCompleted" in out
    assert out.count(correlation_id[:8]) >= 1  # the correlation id appears in the title at least


def test_events_trace_reports_no_events_for_unknown_correlation(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "events", "trace", "nonexistent-correlation"])
    args.func(args)

    out = capsys.readouterr().out
    assert "No events found" in out


def test_events_run_returns_all_events_for_a_run_id(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    repo = EventRepository(cfg)
    correlation_id = new_correlation_id()
    repo.append(
        event_type="DreamStarted", actor="dream", aggregate_type="dream_run",
        correlation_id=correlation_id, run_id=correlation_id,
    )

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "events", "run", correlation_id])
    args.func(args)

    out = capsys.readouterr().out
    assert "DreamStarted" in out
