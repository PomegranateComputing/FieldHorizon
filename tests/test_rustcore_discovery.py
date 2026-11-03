"""
Rust pressure core discovery must not depend on where config.yaml lives.

Regression for the Windows qualification (lot 00): an isolated config kept
outside the repo root resolved cfg.root/fieldhorizon-rust, found nothing,
and every multi-cycle synthesis silently degraded.
"""
from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldhorizon import rustcore
from fieldhorizon.config import AppConfig, load_config
from fieldhorizon.db import init_db
from fieldhorizon.evaluate import CycleEvaluation
from fieldhorizon.events import EventRepository
from fieldhorizon.multicycle import run_multi_cycle
from fieldhorizon.rustcore import (
    RUST_CORE_ENV_VAR,
    SOURCE_CONFIG_ROOT,
    SOURCE_ENV,
    SOURCE_PACKAGE,
    locate_rust_core,
    run_rust_pressure_core,
)

BINARY = "fh-core.exe" if sys.platform == "win32" else "fh-core"


def make_config(root: Path) -> AppConfig:
    return AppConfig(
        root=root,
        database=root / "data.sqlite3",
        books=root / "books",
        json_corpus=root / "json_corpus",
        outputs=root / "outputs",
        logs=root / "logs",
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
        manifestos=root / "manifestos",
        embedding_model="nomic-embed-text",
    )


def make_crate(crate: Path, *, release: bool = False, debug: bool = False) -> Path:
    crate.mkdir(parents=True, exist_ok=True)
    (crate / "Cargo.toml").write_text('[package]\nname = "fh-core"\nversion = "9.9.9"\n', encoding="utf-8")
    for profile, built in (("release", release), ("debug", debug)):
        if built:
            (crate / "target" / profile).mkdir(parents=True, exist_ok=True)
            (crate / "target" / profile / BINARY).write_bytes(b"fake")
    return crate


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv(RUST_CORE_ENV_VAR, raising=False)


def test_a_config_outside_the_repo_resolves_the_package_crate(tmp_path, monkeypatch):
    package_crate = make_crate(tmp_path / "pkg" / "fieldhorizon-rust", release=True)
    monkeypatch.setattr(rustcore, "PACKAGE_RUST_CRATE_DIR", package_crate)
    cfg = make_config(tmp_path / "elsewhere" / "qualification")

    location = locate_rust_core(cfg)

    assert location.source == SOURCE_PACKAGE
    assert location.binary == package_crate / "target" / "release" / BINARY
    assert location.exists


def test_a_crate_vendored_next_to_the_config_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(rustcore, "PACKAGE_RUST_CRATE_DIR", make_crate(tmp_path / "pkg" / "fieldhorizon-rust", release=True))
    cfg_root = tmp_path / "cfg"
    colocated = make_crate(cfg_root / "fieldhorizon-rust", release=True)

    location = locate_rust_core(make_config(cfg_root))

    assert location.source == SOURCE_CONFIG_ROOT
    assert location.crate_dir == colocated


def test_a_directory_without_cargo_toml_is_not_a_vendored_crate(tmp_path, monkeypatch):
    package_crate = make_crate(tmp_path / "pkg" / "fieldhorizon-rust", release=True)
    monkeypatch.setattr(rustcore, "PACKAGE_RUST_CRATE_DIR", package_crate)
    cfg_root = tmp_path / "cfg"
    (cfg_root / "fieldhorizon-rust").mkdir(parents=True)

    assert locate_rust_core(make_config(cfg_root)).source == SOURCE_PACKAGE


def test_the_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(rustcore, "PACKAGE_RUST_CRATE_DIR", make_crate(tmp_path / "pkg" / "fieldhorizon-rust", release=True))
    explicit = tmp_path / "bin" / BINARY
    monkeypatch.setenv(RUST_CORE_ENV_VAR, str(explicit))

    location = locate_rust_core(make_config(tmp_path / "cfg"))

    assert location.source == SOURCE_ENV
    assert location.binary == explicit
    assert not location.exists


def test_release_is_preferred_and_debug_is_the_fallback(tmp_path, monkeypatch):
    both = make_crate(tmp_path / "both" / "fieldhorizon-rust", release=True, debug=True)
    monkeypatch.setattr(rustcore, "PACKAGE_RUST_CRATE_DIR", both)
    assert locate_rust_core(make_config(tmp_path / "cfg")).binary.parent.name == "release"

    debug_only = make_crate(tmp_path / "debug" / "fieldhorizon-rust", debug=True)
    monkeypatch.setattr(rustcore, "PACKAGE_RUST_CRATE_DIR", debug_only)
    assert locate_rust_core(make_config(tmp_path / "cfg")).binary.parent.name == "debug"


def test_an_unbuilt_crate_reports_the_release_path_and_how_it_was_resolved(tmp_path, monkeypatch):
    unbuilt = make_crate(tmp_path / "pkg" / "fieldhorizon-rust")
    monkeypatch.setattr(rustcore, "PACKAGE_RUST_CRATE_DIR", unbuilt)
    cfg = make_config(tmp_path / "cfg")

    location = locate_rust_core(cfg)
    assert location.binary == unbuilt / "target" / "release" / BINARY
    assert not location.exists
    with pytest.raises(FileNotFoundError, match="resolved via package"):
        run_rust_pressure_core(cfg, input_path=tmp_path / "in.json")


def test_the_real_binary_runs_from_a_config_written_outside_the_repo(tmp_path):
    """End to end against this checkout's real fh-core build, if there is one."""
    (tmp_path / "config.yaml").write_text("paths:\n  json_corpus: json_corpus\n", encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    location = locate_rust_core(cfg)
    if not location.exists:
        pytest.skip("fh-core not built (cd fieldhorizon-rust && cargo build --release)")
    assert location.source == SOURCE_PACKAGE

    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        '[{"id": "a", "category": "logos", "tradition": "t", "statement": "s", "severity": 0.9, "mutation_potential": 0.5},'
        ' {"id": "b", "category": "bureaucracy", "tradition": "t", "statement": "s", "severity": 0.8, "mutation_potential": 0.5}]',
        encoding="utf-8",
    )
    report = run_rust_pressure_core(cfg, input_path=corpus, limit=10)
    assert report["node_count"] == 2


def _evaluation() -> CycleEvaluation:
    return CycleEvaluation(
        symbolic_density=1.0, symbolic_density_method="keyword",
        doctrinal_enforcement=1.0, doctrinal_enforcement_method="keyword",
        length_score=1.0, structure_score=1.0, stuffing_penalty=0.0, generic_penalty=0.0,
        final_score=0.6, verdict="HERESY", notes=[],
    )


def _run_dry_multi_cycle(cfg: AppConfig, **patches) -> list:
    with ExitStack() as stack:
        stack.enter_context(patch("fieldhorizon.multicycle.interpret_query", return_value={}))
        stack.enter_context(patch("fieldhorizon.multicycle.interpretation_to_prompt", return_value=""))
        stack.enter_context(patch("fieldhorizon.multicycle.evaluate_cycle", return_value=_evaluation()))
        stack.enter_context(patch("fieldhorizon.multicycle.record_operation_manifest"))
        stack.enter_context(patch("fieldhorizon.multicycle.record_cycle_weather"))
        for target, kwargs in patches.items():
            stack.enter_context(patch(target, **kwargs))
        run_multi_cycle(cfg, "a query", dry_run=True, auto_rewrite=False)
    return [e for e in EventRepository(cfg).recent(limit=50) if e.event_type == "RustPressureComputed"]


def test_a_multi_cycle_records_which_rust_binary_fed_the_synthesis(tmp_path, monkeypatch):
    crate = make_crate(tmp_path / "pkg" / "fieldhorizon-rust", release=True)
    monkeypatch.setattr(rustcore, "PACKAGE_RUST_CRATE_DIR", crate)
    cfg = make_config(tmp_path / "cfg")
    init_db(cfg.database)
    report = tmp_path / "report.json"
    report.write_text('{"node_count": 3, "edge_count": 1, "edges": []}', encoding="utf-8")

    events = _run_dry_multi_cycle(cfg, **{"fieldhorizon.multicycle.export_rust_pressure_report_all": {"return_value": report}})

    assert len(events) == 1
    payload = events[0].payload
    assert payload["status"] == "ok"
    assert payload["resolved_via"] == SOURCE_PACKAGE
    assert payload["binary"] == str(crate / "target" / "release" / BINARY)
    assert payload["binary_sha256"] == rustcore.file_sha256(crate / "target" / "release" / BINARY)
    assert payload["edge_count_total"] == 1


def test_a_degraded_multi_cycle_says_so_in_its_events(tmp_path, monkeypatch):
    monkeypatch.setattr(rustcore, "PACKAGE_RUST_CRATE_DIR", make_crate(tmp_path / "pkg" / "fieldhorizon-rust"))
    cfg = make_config(tmp_path / "cfg")
    init_db(cfg.database)

    events = _run_dry_multi_cycle(
        cfg, **{"fieldhorizon.multicycle.export_rust_pressure_report_all": {"side_effect": FileNotFoundError("gone")}}
    )

    assert len(events) == 1
    assert events[0].payload["status"] == "degraded"
    assert events[0].payload["binary_sha256"] is None
    assert "FileNotFoundError: gone" in events[0].payload["error"]
