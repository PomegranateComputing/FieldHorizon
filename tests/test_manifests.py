from __future__ import annotations

import time
from pathlib import Path

from fieldhorizon.config import AppConfig
from fieldhorizon.db import CURRENT_SCHEMA_VERSION, init_db
from fieldhorizon.manifests import (
    RunManifest,
    RunManifestRepository,
    code_commit,
    configuration_fingerprint,
    db_schema_version,
    dependency_fingerprint,
    ontology_fingerprint,
    rust_binary_fingerprint,
    source_manifest_fingerprint,
)


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


def test_configuration_fingerprint_changes_when_config_yaml_is_modified(tmp_path):
    cfg = make_config(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("a: 1\n", encoding="utf-8")

    before = configuration_fingerprint(cfg)
    time.sleep(0.01)
    config_path.write_text("a: 2\n", encoding="utf-8")
    after = configuration_fingerprint(cfg)

    assert before != after


def test_configuration_fingerprint_is_none_when_file_absent(tmp_path):
    cfg = make_config(tmp_path)
    assert configuration_fingerprint(cfg) is None


def test_source_manifest_fingerprint_changes_when_sources_yaml_is_modified(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    manifest_path = tmp_path / "data" / "sources.yaml"
    manifest_path.write_text("sources: []\n", encoding="utf-8")

    before = source_manifest_fingerprint(cfg)
    time.sleep(0.01)
    manifest_path.write_text("sources: []\n\n# a comment\n", encoding="utf-8")
    after = source_manifest_fingerprint(cfg)

    assert before != after


def test_source_manifest_fingerprint_tolerates_a_malformed_manifest(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "sources.yaml").write_text("not: [valid, yaml, structure: :", encoding="utf-8")

    # Doesn't raise -- falls back to fingerprinting just the manifest file itself.
    assert source_manifest_fingerprint(cfg) is not None


def test_ontology_fingerprint_is_stable_across_repeated_calls():
    first = ontology_fingerprint()
    second = ontology_fingerprint()
    assert first == second
    assert first is not None  # the repo's real ontology.yaml exists


def test_dependency_fingerprint_is_stable_and_present():
    first = dependency_fingerprint()
    second = dependency_fingerprint()
    assert first == second
    assert first is not None  # the repo's real pyproject.toml exists


def test_rust_binary_fingerprint_reports_version_even_without_a_built_binary(tmp_path):
    cfg = make_config(tmp_path)
    (tmp_path / "fieldhorizon-rust").mkdir(parents=True, exist_ok=True)
    (tmp_path / "fieldhorizon-rust" / "Cargo.toml").write_text(
        '[package]\nname = "fh-core"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    result = rust_binary_fingerprint(cfg)
    assert result == "version=0.1.0"


def test_rust_binary_fingerprint_is_none_with_no_cargo_toml_and_no_binary(tmp_path):
    cfg = make_config(tmp_path)
    assert rust_binary_fingerprint(cfg) is None


def test_code_commit_never_raises_and_returns_unknown_outside_a_repo(tmp_path):
    cfg = make_config(tmp_path)
    # tmp_path is not a git repository.
    assert code_commit(cfg) == "unknown"


def test_db_schema_version_reflects_the_current_schema(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    # Asserted against the constant, not a literal: this test's subject is
    # that init_db STAMPS the current version, not what that version
    # happens to be this month. Hardcoding the number made every
    # schema-affecting phase fail a test that was working correctly.
    assert db_schema_version(cfg) == CURRENT_SCHEMA_VERSION


def test_run_manifest_round_trips_through_the_repository(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    manifest = RunManifest(
        run_id="corr-123",
        operation="cycle",
        input_fingerprint="abc",
        ontology_fingerprint="def",
        db_schema_version=1,
        code_commit="deadbee",
        random_seeds={"schools_seed": 42},
        selected_evidence_ids=["book:1", "json:2"],
        output_ids=[99],
    )

    repo = RunManifestRepository(cfg)
    repo.record(manifest)

    fetched = repo.get("corr-123")
    assert fetched is not None
    assert fetched.operation == "cycle"
    assert fetched.random_seeds == {"schools_seed": 42}
    assert fetched.selected_evidence_ids == ["book:1", "json:2"]
    assert fetched.output_ids == [99]


def test_run_manifest_get_returns_none_for_unknown_run_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert RunManifestRepository(cfg).get("nonexistent") is None


def test_run_manifest_recent_filters_by_operation(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = RunManifestRepository(cfg)
    repo.record(RunManifest(run_id="r1", operation="cycle"))
    repo.record(RunManifest(run_id="r2", operation="council"))
    repo.record(RunManifest(run_id="r3", operation="cycle"))

    cycles = repo.recent(operation="cycle")
    assert {m.run_id for m in cycles} == {"r1", "r3"}
    assert len(repo.recent()) == 3
