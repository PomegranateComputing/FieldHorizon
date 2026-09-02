from __future__ import annotations

import json
from pathlib import Path

from fieldhorizon.cli import build_parser
from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.events import EventRepository
from fieldhorizon.provenance import (
    OBJECT_CYCLE,
    OBJECT_JSON_ENTRY,
    OBJECT_SCHOOL,
    REL_MEMBER_OF,
    REL_REHABILITATES,
    REL_RETIRES,
    REL_SELECTED_BY,
    REL_SUPERSEDES,
    REL_SUPPORTS,
    ProvenanceEdgeRepository,
    backfill_provenance_edges,
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


def _insert_cycle(cfg: AppConfig, parent_cycle_ids: list[int] | None = None) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, parent_cycle_ids) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'f', ?)",
            (json.dumps(parent_cycle_ids) if parent_cycle_ids else None,),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_backfill_reconstructs_selected_by_from_parent_cycle_ids(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    parent_id = _insert_cycle(cfg)
    child_id = _insert_cycle(cfg, parent_cycle_ids=[parent_id])

    report = backfill_provenance_edges(cfg)

    assert report.edges_added > 0
    [edge] = ProvenanceEdgeRepository(cfg).edges_to(OBJECT_CYCLE, child_id, relation_type=REL_SELECTED_BY)
    assert edge.source_id == str(parent_id)
    assert edge.creation_method == "backfill"


def test_backfill_reconstructs_supports_from_cycle_sources(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle(cfg)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (?, 'json', 'axiom_1', 'c')",
            (cycle_id,),
        )
        conn.commit()

    backfill_provenance_edges(cfg)

    [edge] = ProvenanceEdgeRepository(cfg).edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_SUPPORTS)
    assert edge.source_type == OBJECT_JSON_ENTRY
    assert edge.source_id == "axiom_1"


def test_backfill_reconstructs_member_of_and_supersedes(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle(cfg)
    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO schools(name, summary, run_at) VALUES ('S1', 'sum', '2024-01-01')")
        first_school_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO schools(name, summary, previous_school_id, run_at) VALUES ('S2', 'sum', ?, '2024-01-02')",
            (first_school_id,),
        )
        second_school_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO school_members(school_id, cycle_id, distance) VALUES (?, ?, 0.1)",
            (second_school_id, cycle_id),
        )
        conn.commit()

    backfill_provenance_edges(cfg)

    repo = ProvenanceEdgeRepository(cfg)
    [member_edge] = repo.edges_to(OBJECT_SCHOOL, second_school_id, relation_type=REL_MEMBER_OF)
    assert member_edge.source_id == str(cycle_id)

    [supersedes_edge] = repo.edges_from(OBJECT_SCHOOL, second_school_id, relation_type=REL_SUPERSEDES)
    assert supersedes_edge.target_id == str(first_school_id)


def test_backfill_reconstructs_rehabilitates_when_attributable_via_domain_events(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle(cfg)

    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO canon_events(cycle_id, event, detail) VALUES (?, 'REHABILITATED', '{}')", (cycle_id,))
        canon_event_id = int(cur.lastrowid)
        conn.commit()

    events = EventRepository(cfg)
    events.append(
        event_type="CouncilStarted", actor="system", aggregate_type="council",
        correlation_id="corr-1", aggregate_id=99,
    )
    events.append(
        event_type="FragmentRehabilitated", actor="system", aggregate_type="cycle",
        correlation_id="corr-1", aggregate_id=cycle_id, canon_event_id=canon_event_id,
    )

    report = backfill_provenance_edges(cfg)

    assert report.unattributed_canon_events == 0
    [edge] = ProvenanceEdgeRepository(cfg).edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_REHABILITATES)
    assert edge.source_id == "99"


def test_backfill_reports_unattributed_canon_events_predating_domain_events(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle(cfg)

    with connect(cfg.database) as conn:
        conn.execute("INSERT INTO canon_events(cycle_id, event, detail) VALUES (?, 'RETIRED', '{}')", (cycle_id,))
        conn.commit()

    report = backfill_provenance_edges(cfg)

    assert report.unattributed_canon_events == 1
    assert ProvenanceEdgeRepository(cfg).edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_RETIRES) == []


def test_backfill_is_idempotent(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    parent_id = _insert_cycle(cfg)
    _insert_cycle(cfg, parent_cycle_ids=[parent_id])

    first = backfill_provenance_edges(cfg)
    second = backfill_provenance_edges(cfg)

    assert first.edges_added > 0
    assert second.edges_added == 0
    assert second.edges_before == second.edges_after == first.edges_after


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


def test_provenance_backfill_cli_reports_edges_added(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    parent_id = _insert_cycle(cfg)
    _insert_cycle(cfg, parent_cycle_ids=[parent_id])

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "provenance", "backfill"])
    args.func(args)

    out = capsys.readouterr().out
    assert "Provenance edges recorded" in out


def test_provenance_backfill_cli_reports_unresolved_and_unattributed_gaps(tmp_path, capsys):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    config_path = _write_config_yaml(tmp_path)
    cycle_id = _insert_cycle(cfg)
    with connect(cfg.database) as conn:
        conn.execute("INSERT INTO canon_events(cycle_id, event, detail) VALUES (?, 'RETIRED', '{}')", (cycle_id,))
        conn.commit()

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "provenance", "backfill"])
    args.func(args)

    out = capsys.readouterr().out
    assert "predate domain_events" in out
    assert "1 canon_events row" in out
