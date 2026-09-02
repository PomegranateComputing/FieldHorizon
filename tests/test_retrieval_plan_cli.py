from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fieldhorizon.cli import build_parser
from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db


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


def test_retrieve_plan_cli_reports_the_plan(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    parser = build_parser()
    args = parser.parse_args(
        ["--config", str(config_path), "retrieve", "a plain description of a garden", "--plan"]
    )
    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        args.func(args)

    out = capsys.readouterr().out
    assert "Retrieval plan" in out
    assert "LEXICAL" in out
    assert "Constraints applied" in out
    assert "No candidates found" in out


def test_retrieve_plan_cli_shows_contradiction_for_an_oppositional_query(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    parser = build_parser()
    args = parser.parse_args(
        ["--config", str(config_path), "retrieve", "the truth of logos and the bureaucracy of compliance metrics", "--plan"]
    )
    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        args.func(args)

    out = capsys.readouterr().out
    assert "CONTRADICTION" in out


def _insert_chunk(cfg: AppConfig, content: str) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('t', '/p', 'book')")
        source_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
            "VALUES (?, 0, 'ref', ?, 1)",
            (source_id, content),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_retrieve_plan_cli_with_explain_shows_component_tables(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    chunk_id = _insert_chunk(cfg, "some content")
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO chunk_entities(chunk_id, entity, kind) VALUES (?, 'babel', 'place')", (chunk_id,)
        )
        conn.commit()

    parser = build_parser()
    args = parser.parse_args(
        ["--config", str(config_path), "retrieve", "the tower of babel", "--plan", "--explain"]
    )
    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model")):
        args.func(args)

    out = capsys.readouterr().out
    assert "Chunk candidates" in out
    assert "entity_match" in out
