from __future__ import annotations

import json
from pathlib import Path

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.lineage import LineageNotFoundError
from fieldhorizon.provenance import (
    OBJECT_CHUNK,
    OBJECT_CYCLE,
    OBJECT_JSON_ENTRY,
    REL_EXTRACTED_FROM,
    REL_MUTATES,
    REL_SELECTED_BY,
    REL_SUPPORTS,
    ProvenanceEdgeRepository,
    add_provenance_graph_section,
    build_provenance_report,
    build_provenance_report_for_target,
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


def _insert_axiom(cfg: AppConfig, entry_id: str) -> None:
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO json_entries(id, group_name, category, statement, raw_json) VALUES (?, 'g', 'c', 's', '{}')",
            (entry_id,),
        )
        conn.commit()


def _insert_cycle(cfg: AppConfig, cycle_id: int, parent_cycle_ids: list[int] | None = None) -> None:
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycles(id, query, model, prompt, response, dry_run, verdict, final_score, fragment, parent_cycle_ids) "
            "VALUES (?, 'q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'f', ?)",
            (cycle_id, json.dumps(parent_cycle_ids) if parent_cycle_ids else None),
        )
        conn.commit()


def _insert_chunk(cfg: AppConfig, canonical_ref: str) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('S', '/tmp/s.txt', 'book')")
        source_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) VALUES (?, 0, ?, 'c', 1)",
            (source_id, canonical_ref),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_circular_ancestry_detector_catches_a_synthetic_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_axiom(cfg, "axiom_a")
    _insert_axiom(cfg, "axiom_b")

    repo = ProvenanceEdgeRepository(cfg)
    repo.append(OBJECT_JSON_ENTRY, "axiom_a", OBJECT_JSON_ENTRY, "axiom_b", REL_MUTATES)
    repo.append(OBJECT_JSON_ENTRY, "axiom_b", OBJECT_JSON_ENTRY, "axiom_a", REL_MUTATES)

    report = build_provenance_report(cfg, OBJECT_JSON_ENTRY, "axiom_a")
    assert report.circular_ancestry is True


def test_a_diamond_shared_ancestor_is_not_mistaken_for_a_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_cycle(cfg, 1)
    _insert_cycle(cfg, 2)
    _insert_cycle(cfg, 3, parent_cycle_ids=[1, 2])
    _insert_axiom(cfg, "shared_axiom")

    repo = ProvenanceEdgeRepository(cfg)
    repo.append(OBJECT_CYCLE, 1, OBJECT_CYCLE, 3, REL_SELECTED_BY)
    repo.append(OBJECT_CYCLE, 2, OBJECT_CYCLE, 3, REL_SELECTED_BY)
    repo.append(OBJECT_JSON_ENTRY, "shared_axiom", OBJECT_CYCLE, 1, REL_SUPPORTS)
    repo.append(OBJECT_JSON_ENTRY, "shared_axiom", OBJECT_CYCLE, 2, REL_SUPPORTS)

    report = build_provenance_report(cfg, OBJECT_CYCLE, 3)
    assert report.circular_ancestry is False


def test_synthetic_dependency_ratio_counts_ungrounded_leaves(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_cycle(cfg, 1)
    chunk_id = _insert_chunk(cfg, "ref-1")
    _insert_axiom(cfg, "ungrounded_axiom")

    repo = ProvenanceEdgeRepository(cfg)
    repo.append(OBJECT_CHUNK, chunk_id, OBJECT_CYCLE, 1, REL_SUPPORTS)
    repo.append(OBJECT_JSON_ENTRY, "ungrounded_axiom", OBJECT_CYCLE, 1, REL_SUPPORTS)

    report = build_provenance_report(cfg, OBJECT_CYCLE, 1)
    assert report.synthetic_dependency_ratio == 0.5


def test_supply_chain_flags_a_fully_generated_only_dependency(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_cycle(cfg, 1)
    _insert_axiom(cfg, "generated_only")

    ProvenanceEdgeRepository(cfg).append(OBJECT_JSON_ENTRY, "generated_only", OBJECT_CYCLE, 1, REL_SUPPORTS)

    report = build_provenance_report(cfg, OBJECT_CYCLE, 1)
    assert report.synthetic_dependency_ratio == 1.0


def test_extracted_from_grounds_an_axiom_leaf_as_sourced(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_cycle(cfg, 1)
    _insert_axiom(cfg, "grounded_axiom")
    chunk_id = _insert_chunk(cfg, "ref-1")

    repo = ProvenanceEdgeRepository(cfg)
    repo.append(OBJECT_JSON_ENTRY, "grounded_axiom", OBJECT_CYCLE, 1, REL_SUPPORTS)
    repo.append(OBJECT_JSON_ENTRY, "grounded_axiom", OBJECT_CHUNK, chunk_id, REL_EXTRACTED_FROM)

    report = build_provenance_report(cfg, OBJECT_CYCLE, 1)
    assert report.synthetic_dependency_ratio == 0.0


def test_weakest_link_is_the_lowest_confidence_edge(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_cycle(cfg, 1)
    _insert_axiom(cfg, "axiom_1")
    _insert_axiom(cfg, "axiom_2")

    repo = ProvenanceEdgeRepository(cfg)
    repo.append(OBJECT_JSON_ENTRY, "axiom_1", OBJECT_CYCLE, 1, REL_SUPPORTS, confidence=1.0)
    repo.append(OBJECT_JSON_ENTRY, "axiom_2", OBJECT_CYCLE, 1, REL_SUPPORTS, confidence=0.3)

    report = build_provenance_report(cfg, OBJECT_CYCLE, 1)
    assert report.weakest_link is not None
    assert report.weakest_link.confidence == 0.3


def test_completeness_score_on_a_known_incomplete_fixture(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_cycle(cfg, 1)
    _insert_cycle(cfg, 2, parent_cycle_ids=[1])
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (2, 'json', 'axiom_1', 'c')"
        )
        conn.commit()

    # Only the SELECTED_BY edge is recorded -- the SUPPORTS edge for the
    # json cycle_sources row is deliberately missing.
    ProvenanceEdgeRepository(cfg).append(OBJECT_CYCLE, 1, OBJECT_CYCLE, 2, REL_SELECTED_BY)

    report = build_provenance_report(cfg, OBJECT_CYCLE, 2)
    assert report.completeness_score == 0.5
    assert len(report.missing_links) == 1
    assert "axiom_1" in report.missing_links[0]


def test_completeness_score_is_vacuously_complete_when_nothing_was_expected(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_cycle(cfg, 1)

    report = build_provenance_report(cfg, OBJECT_CYCLE, 1)
    assert report.completeness_score == 1.0
    assert report.missing_links == []


def test_opposing_evidence_count_is_always_zero_today(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_cycle(cfg, 1)

    report = build_provenance_report(cfg, OBJECT_CYCLE, 1)
    assert report.opposing_evidence_count == 0


def test_build_provenance_report_for_target_dispatches_on_numeric_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_cycle(cfg, 42)
    _insert_axiom(cfg, "axiom_1")

    assert build_provenance_report_for_target(cfg, "42").object_type == OBJECT_CYCLE
    assert build_provenance_report_for_target(cfg, "axiom_1").object_type == OBJECT_JSON_ENTRY


def test_build_provenance_report_for_target_raises_not_found_for_a_nonexistent_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with pytest.raises(LineageNotFoundError):
        build_provenance_report_for_target(cfg, "999999")


def test_build_provenance_report_for_target_raises_not_found_for_a_nonexistent_axiom(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with pytest.raises(LineageNotFoundError):
        build_provenance_report_for_target(cfg, "nonexistent_axiom_id_xyz")


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


def test_provenance_show_cli_reports_the_supply_chain(tmp_path, capsys):
    from fieldhorizon.cli import build_parser

    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    _insert_cycle(cfg, 1)
    _insert_axiom(cfg, "axiom_1")
    ProvenanceEdgeRepository(cfg).append(OBJECT_JSON_ENTRY, "axiom_1", OBJECT_CYCLE, 1, REL_SUPPORTS, confidence=0.4)

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "provenance", "show", "1"])
    args.func(args)

    out = capsys.readouterr().out
    assert "Epistemic supply chain" in out
    assert "Weakest link" in out
    assert "always 0 today" in out


def test_provenance_show_cli_reports_missing_links(tmp_path, capsys):
    from fieldhorizon.cli import build_parser

    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    _insert_cycle(cfg, 1)
    _insert_cycle(cfg, 2, parent_cycle_ids=[1])
    # No SELECTED_BY edge recorded for the parent -- a known-incomplete fixture.

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "provenance", "show", "2"])
    args.func(args)

    out = capsys.readouterr().out
    assert "Missing links" in out
    assert "parent cycle 1" in out


def test_add_provenance_graph_section_is_a_no_op_without_edges(tmp_path):
    from rich.tree import Tree

    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_cycle(cfg, 1)

    tree = Tree("root")
    add_provenance_graph_section(tree, cfg, OBJECT_CYCLE, 1)
    assert len(tree.children) == 0


def test_add_provenance_graph_section_lists_outgoing_and_incoming_edges(tmp_path):
    from rich.console import Console
    from rich.tree import Tree

    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_cycle(cfg, 1)
    _insert_cycle(cfg, 2)

    repo = ProvenanceEdgeRepository(cfg)
    repo.append(OBJECT_CYCLE, 1, OBJECT_CYCLE, 2, REL_SELECTED_BY)  # incoming to 1's perspective: 1 -> 2 is outgoing from 1

    tree = Tree("root")
    add_provenance_graph_section(tree, cfg, OBJECT_CYCLE, 1)

    console = Console(record=True, width=200)
    console.print(tree)
    out = console.export_text()

    assert "Provenance graph" in out
    assert "Outgoing" in out
    assert f"{REL_SELECTED_BY} -> cycle:2" in out


def test_lineage_cli_includes_the_provenance_graph_section(tmp_path, capsys):
    from fieldhorizon.cli import build_parser

    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    _insert_cycle(cfg, 1)
    _insert_cycle(cfg, 2)
    ProvenanceEdgeRepository(cfg).append(OBJECT_CYCLE, 1, OBJECT_CYCLE, 2, REL_SELECTED_BY)

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "lineage", "1"])
    args.func(args)

    out = capsys.readouterr().out
    assert "Provenance graph" in out
    assert "Outgoing" in out
