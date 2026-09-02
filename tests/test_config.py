from __future__ import annotations

from fieldhorizon.config import load_config

MINIMAL_CONFIG = """
paths:
  database: "data/field_horizon.sqlite3"
  books: "data/books"
  json_corpus: "data/json_corpus"
  outputs: "outputs"
  logs: "logs"
  manifestos: "data/manifestos"
"""

LEGACY_MANIFESTOS_CONFIG = """
paths:
  database: "data/field_horizon.sqlite3"
  books: "data/books"
  json_corpus: "data/json_corpus"
  outputs: "outputs"
  logs: "logs"

manifestos: "data/legacy_manifestos"
"""


def test_root_is_derived_from_config_file_location_not_cwd(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    cfg_path = project_dir / "config.yaml"
    cfg_path.write_text(MINIMAL_CONFIG, encoding="utf-8")

    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    cfg = load_config(str(cfg_path))

    assert cfg.root == project_dir
    assert cfg.database == project_dir / "data" / "field_horizon.sqlite3"
    assert cfg.books == project_dir / "data" / "books"


def test_relative_config_path_still_resolves_against_cwd(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "config.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")

    monkeypatch.chdir(project_dir)
    cfg = load_config("config.yaml")

    assert cfg.root == project_dir


def test_manifestos_reads_from_paths_section(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_CONFIG, encoding="utf-8")

    cfg = load_config(str(cfg_path))

    assert cfg.manifestos == tmp_path / "data" / "manifestos"


def test_legacy_top_level_manifestos_still_works_with_warning(tmp_path, caplog):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(LEGACY_MANIFESTOS_CONFIG, encoding="utf-8")

    with caplog.at_level("WARNING", logger="fieldhorizon.config"):
        cfg = load_config(str(cfg_path))

    assert cfg.manifestos == tmp_path / "data" / "legacy_manifestos"
    assert any("deprecated" in record.message for record in caplog.records)


def test_retrieval_policy_defaults_when_unconfigured(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(MINIMAL_CONFIG, encoding="utf-8")

    cfg = load_config(str(cfg_path))

    assert cfg.retrieval_policy.max_per_source == 3
    assert cfg.retrieval_policy.min_direct_evidence_count == 1
    assert cfg.retrieval_policy.max_synthetic_evidence_ratio == 0.9


def test_retrieval_policy_reads_from_config_yaml(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        MINIMAL_CONFIG
        + """
retrieval:
  policy:
    max_per_source: 2
    min_direct_evidence_count: 3
    max_synthetic_evidence_ratio: 0.5
""",
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_path))

    assert cfg.retrieval_policy.max_per_source == 2
    assert cfg.retrieval_policy.min_direct_evidence_count == 3
    assert cfg.retrieval_policy.max_synthetic_evidence_ratio == 0.5
