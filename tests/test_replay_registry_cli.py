from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fieldhorizon.cli import build_parser
from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.embeddings import store_cycle_embedding
from fieldhorizon.manifests import RunManifest, RunManifestRepository, build_manifest_fingerprints
from fieldhorizon.registries import get_or_register_model


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


def insert_cycle(cfg: AppConfig, prompt: str, model: str, fragment: str) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q', ?, ?, 'r', 0, 'CANON', 0.9, ?)",
            (model, prompt, fragment),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_replay_level2_cli_reports_no_drift(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    cycle_id = insert_cycle(cfg, "prompt", "m", "fragment")

    fingerprints = build_manifest_fingerprints(cfg)
    RunManifestRepository(cfg).record(RunManifest(run_id="corr-1", operation="cycle", output_ids=[cycle_id], **fingerprints))

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "replay", str(cycle_id), "--level", "2"])
    args.func(args)

    out = capsys.readouterr().out
    assert "L2 configuration replay" in out
    assert "No drift" in out


def test_replay_level2_cli_reports_missing_manifest(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    cycle_id = insert_cycle(cfg, "prompt", "m", "fragment")

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "replay", str(cycle_id), "--level", "2"])

    try:
        args.func(args)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert exc.code == 1
    assert "No run_manifests row" in capsys.readouterr().out


def test_replay_level3_cli_reconstructs_evidence(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    cycle_id = insert_cycle(cfg, "prompt", "m", "fragment")

    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (?, 'book', 'ref-1', 'book content')",
            (cycle_id,),
        )
        conn.commit()

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "replay", str(cycle_id), "--level", "3"])
    args.func(args)

    out = capsys.readouterr().out
    assert "L3 evidence replay" in out
    assert "ref-1" in out
    assert "book content" in out


def _run_schools_for_cli(cfg: AppConfig) -> str:
    import json as _json
    from contextlib import ExitStack

    from fieldhorizon.schools import run_schools

    for i in range(3):
        insert_canon_cycle = insert_cycle(cfg, f"q{i}", "m", f"fragment about the machine as idol {i}")
        store_cycle_embedding(cfg, insert_canon_cycle, [1.0, 0.0, 0.0, 0.0], "nomic-embed-text", "nomic-embed-text")
    for i in range(3):
        insert_canon_cycle = insert_cycle(cfg, f"r{i}", "m", f"fragment about gardens and rivers {i}")
        store_cycle_embedding(cfg, insert_canon_cycle, [0.0, 1.0, 0.0, 0.0], "nomic-embed-text", "nomic-embed-text")

    raw = _json.dumps({"name": "A School", "summary": "Two sentences. About doctrine."})
    stack = ExitStack()
    stack.enter_context(patch("fieldhorizon.embeddings.current_embedding_version", return_value="nomic-embed-text"))
    stack.enter_context(patch("fieldhorizon.schools.call_ollama", return_value=raw))
    with stack:
        run_schools(cfg, k=2, seed=7)

    manifests = RunManifestRepository(cfg).recent(operation="schools", limit=1)
    return manifests[0].run_id


def test_replay_level4_cli_reports_an_identical_partition(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    run_id = _run_schools_for_cli(cfg)

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "replay", run_id, "--level", "4"])
    with patch("fieldhorizon.embeddings.current_embedding_version", return_value="nomic-embed-text"):
        args.func(args)

    out = capsys.readouterr().out
    assert "L4 deterministic-stage replay" in out
    assert "Identical partition" in out


def test_replay_level4_cli_reports_an_unknown_run_id(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "replay", "no-such-run", "--level", "4"])

    try:
        args.func(args)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert exc.code == 1
    assert "No schools run_manifests row" in capsys.readouterr().out


def test_replay_level5_cli_diffs_baseline_against_variant(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    cycle_id = insert_cycle(cfg, "prompt", "hermes3:8b", "original fragment")

    def fake_generate(cfg, prompt, model):
        return "baseline text" if model == "hermes3:8b" else "variant text"

    parser = build_parser()
    args = parser.parse_args(
        ["--config", str(config_path), "replay", str(cycle_id), "--level", "5", "--variant-model", "qwen3:8b"]
    )
    with patch("fieldhorizon.replay.generate_fragment", side_effect=fake_generate):
        args.func(args)

    out = capsys.readouterr().out
    assert "L5 comparative replay" in out
    assert "baseline text" in out
    assert "variant text" in out


def test_replay_level5_cli_requires_variant_model(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    cycle_id = insert_cycle(cfg, "prompt", "hermes3:8b", "original fragment")

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "replay", str(cycle_id), "--level", "5"])

    try:
        args.func(args)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert exc.code == 1
    assert "--variant-model is required" in capsys.readouterr().out


def test_registry_list_reports_no_entries_on_a_fresh_db(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "registry", "list", "model"])
    args.func(args)

    out = capsys.readouterr().out
    assert "No model registry entries" in out


def test_registry_list_and_show_round_trip(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    with patch("fieldhorizon.registries.requests.get", side_effect=RuntimeError("no ollama")):
        model_id = get_or_register_model(cfg, "hermes3:8b")

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "registry", "list", "model"])
    args.func(args)
    out = capsys.readouterr().out
    assert model_id in out

    args = parser.parse_args(["--config", str(config_path), "registry", "show", "model", model_id])
    args.func(args)
    out = capsys.readouterr().out
    assert "hermes3:8b" in out


def test_registry_show_reports_unknown_entry(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "registry", "show", "model", "no-such-id"])

    try:
        args.func(args)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert exc.code == 1
    assert "No model registry entry" in capsys.readouterr().out
