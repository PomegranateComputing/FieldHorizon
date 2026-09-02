from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from fieldhorizon.cli import build_parser
from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.server import create_app
from fieldhorizon.temporal import TemporalCanonRepository

# Implementation Brief IV, Phase UI-2 item 5: CLI and API are two presentations
# of the same FieldHorizonEngine facade (FABLE Sec.8.2 -- "the CLI and the API
# must call the same application layer"). These tests seed one database and
# check both entry points report the same underlying facts, not just that
# neither one crashes.

TOKEN = "test-token-abc123"


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


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_canon_cli_and_api_report_the_same_active_entry(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('the query', 'm', 'p', 'r', 0, 'CANON', 0.9, 'the fragment')"
        )
        cycle_id = int(cur.lastrowid)
        conn.commit()
    TemporalCanonRepository(cfg).append(cycle_id, "CANON", 0.9, valid_from="2024-01-01 00:00:00")

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "canon"])
    args.func(args)
    cli_out = capsys.readouterr().out
    assert str(cycle_id) in cli_out
    assert "the query" in cli_out
    assert "CANON" in cli_out

    client = TestClient(create_app(cfg, TOKEN))
    body = client.get("/canon", headers=auth_headers()).json()
    assert body["entries"][0]["cycle_id"] == cycle_id
    assert body["entries"][0]["query"] == "the query"
    assert body["entries"][0]["fragment"] == "the fragment"
    assert body["entries"][0]["verdict"] == "CANON"


def test_weather_as_of_cli_and_api_report_the_same_reading(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO weather_readings(recorded_at, scope, axis, value) "
            "VALUES ('2024-01-01 00:00:00', 'canon', 'tawhid', 0.5)"
        )
        conn.commit()

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "weather-as-of", "2024-02-01 00:00:00"])
    args.func(args)
    cli_out = capsys.readouterr().out
    assert "tawhid" in cli_out
    assert "0.500" in cli_out

    client = TestClient(create_app(cfg, TOKEN))
    body = client.get("/weather", params={"as_of": "2024-02-01 00:00:00"}, headers=auth_headers()).json()
    assert body["canon"]["tawhid"] == 0.5


def test_registry_list_cli_and_api_report_the_same_entries(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO model_registry(model_id, name, digest, active, first_seen_at) "
            "VALUES ('m1', 'hermes3:8b', 'sha256:abc', 1, '2026-01-01T00:00:00')"
        )
        conn.commit()

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "registry", "list", "model"])
    args.func(args)
    cli_out = capsys.readouterr().out
    assert "hermes3:8b" in cli_out

    client = TestClient(create_app(cfg, TOKEN))
    body = client.get("/registry/model", headers=auth_headers()).json()
    assert body["entries"][0]["name"] == "hermes3:8b"
