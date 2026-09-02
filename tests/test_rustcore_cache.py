from __future__ import annotations

import time
from unittest.mock import patch

from fieldhorizon.config import AppConfig
from fieldhorizon.rustcore import export_rust_pressure_report_all, json_corpus_fingerprint


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


def fake_rust_core(cfg, input_path, limit=50):
    return {"node_count": 1, "edge_count": 0, "edges": []}


def test_fingerprint_changes_when_a_corpus_file_is_modified(tmp_path):
    cfg = make_config(tmp_path)
    cfg.json_corpus.mkdir(parents=True, exist_ok=True)
    (cfg.json_corpus / "a.json").write_text("[]", encoding="utf-8")

    before = json_corpus_fingerprint(cfg)

    time.sleep(0.01)
    (cfg.json_corpus / "a.json").write_text('[{"id": "x"}]', encoding="utf-8")

    after = json_corpus_fingerprint(cfg)
    assert before != after


def test_repeated_calls_do_not_rerun_the_binary_when_corpus_is_unchanged(tmp_path):
    cfg = make_config(tmp_path)
    cfg.json_corpus.mkdir(parents=True, exist_ok=True)
    (cfg.json_corpus / "a.json").write_text('[{"id": "x", "statement": "s"}]', encoding="utf-8")

    with patch("fieldhorizon.rustcore.run_rust_pressure_core", side_effect=fake_rust_core) as mock_run:
        export_rust_pressure_report_all(cfg, limit=10)
        export_rust_pressure_report_all(cfg, limit=10)
        export_rust_pressure_report_all(cfg, limit=10)

    assert mock_run.call_count == 1


def test_changing_the_corpus_triggers_regeneration(tmp_path):
    cfg = make_config(tmp_path)
    cfg.json_corpus.mkdir(parents=True, exist_ok=True)
    (cfg.json_corpus / "a.json").write_text('[{"id": "x", "statement": "s"}]', encoding="utf-8")

    with patch("fieldhorizon.rustcore.run_rust_pressure_core", side_effect=fake_rust_core) as mock_run:
        export_rust_pressure_report_all(cfg, limit=10)

        (cfg.json_corpus / "b.json").write_text('[{"id": "y", "statement": "t"}]', encoding="utf-8")
        export_rust_pressure_report_all(cfg, limit=10)

    assert mock_run.call_count == 2


def test_changing_the_limit_triggers_regeneration(tmp_path):
    cfg = make_config(tmp_path)
    cfg.json_corpus.mkdir(parents=True, exist_ok=True)
    (cfg.json_corpus / "a.json").write_text('[{"id": "x", "statement": "s"}]', encoding="utf-8")

    with patch("fieldhorizon.rustcore.run_rust_pressure_core", side_effect=fake_rust_core) as mock_run:
        export_rust_pressure_report_all(cfg, limit=10)
        export_rust_pressure_report_all(cfg, limit=25)

    assert mock_run.call_count == 2
