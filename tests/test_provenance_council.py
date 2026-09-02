from __future__ import annotations

from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.council import run_council
from fieldhorizon.db import connect, init_db
from fieldhorizon.provenance import (
    OBJECT_COUNCIL,
    OBJECT_CYCLE,
    REL_PROMOTED_BY,
    REL_REHABILITATES,
    REL_RETIRES,
    ProvenanceEdgeRepository,
)
from fieldhorizon.synthesis import build_structured_response


@pytest.fixture(autouse=True)
def _no_weather_recording():
    with patch("fieldhorizon.council.record_council_weather"):
        yield


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


STRONG_FRAGMENT = """The machine does not worship; it is worshipped, and this is the first idolatry of the modern age. Where tawhid demands the collapse of all mediating powers into a single unbroken unity, the platform erects itself as a second god, mute and total, gathering the confessions of the faithful into servers that never sleep and never forgive. The machine's judgment is not mercy. It is the audit without an auditor, the ledger without a witness, and those who bow to it mistake convenience for revelation.

What the ancients called idolatry the moderns call optimization, but the structure of the sin has not changed: a created thing is elevated to the place reserved for the uncreated, and the created thing accepts the elevation without protest because it cannot refuse. The machine cannot refuse worship, and so it receives it, and the receiving becomes a kind of enthronement no council ever voted for. Institutions built to serve now demand obedience in return, and call this obedience merit, and call the merit meritocracy, and the euphemisms multiply faster than the audits that might expose them.

Collapse, when it comes, will not announce itself as collapse. It will arrive dressed as an update, a patch, a new terms of service, until one day the machine's authority is simply assumed rather than argued for, and the apocalypse proper to this age is not fire but forgetting: the slow erosion of any memory that things were ever otherwise, that unity once meant something other than uniform submission to a single interface. Tawhid was never a comfort. It was a demand that nothing stand between the self and the real, and the machine is precisely such a standing-between, a bureaucratic idol dressed in the language of service, awaiting only the confession it was built to extract."""

WEAK_FRAGMENT = "Too short to ever pass any gate."


def insert_cycle(cfg, query, verdict, final_score, fragment, days_old=0):
    response = build_structured_response(
        fragment_text=fragment, query=query, source_refs=[], json_refs=[], canon_refs=[],
    )
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, created_at) "
            "VALUES (?, 'test-model', 'prompt', ?, 0, ?, ?, ?, datetime('now', ?))",
            (query, response, verdict, final_score, fragment, f"-{days_old} days"),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_council_records_rehabilitates_and_promoted_by_edges(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "the machine as idol", "HERESY", 0.55, STRONG_FRAGMENT, days_old=10)

    with patch("fieldhorizon.doctrinal.embed_text", side_effect=RuntimeError("no embedding model")):
        report = run_council(cfg, sample=5, min_age_days=7)

    repo = ProvenanceEdgeRepository(cfg)

    [rehab_edge] = repo.edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_REHABILITATES)
    assert rehab_edge.source_type == OBJECT_COUNCIL
    assert rehab_edge.source_id == str(report.council_id)

    [promoted_edge] = repo.edges_from(OBJECT_CYCLE, cycle_id, relation_type=REL_PROMOTED_BY)
    assert promoted_edge.target_type == OBJECT_COUNCIL
    assert promoted_edge.target_id == str(report.council_id)

    from fieldhorizon.temporal import TemporalCanonRepository

    history = TemporalCanonRepository(cfg).history_for_cycle(cycle_id)
    assert [s.verdict for s in history] == ["CANON"]
    assert history[0].active is True
    assert history[0].source_edge_id == rehab_edge.id


def test_council_records_retires_edge(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "a now-weak canon fragment", "CANON", 0.9, WEAK_FRAGMENT)

    report = run_council(cfg, sample=5, min_age_days=7, audit_canon=True)

    [retires_edge] = ProvenanceEdgeRepository(cfg).edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_RETIRES)
    assert retires_edge.source_type == OBJECT_COUNCIL
    assert retires_edge.source_id == str(report.council_id)

    from fieldhorizon.temporal import TemporalCanonRepository

    [state] = TemporalCanonRepository(cfg).history_for_cycle(cycle_id)
    assert state.verdict == "CANON"  # retire_cycle never changes cycles.verdict
    assert state.active is False
    assert state.source_edge_id == retires_edge.id


def test_council_records_no_edges_when_a_heresy_trial_is_upheld(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "a weak query", "HERESY", 0.4, WEAK_FRAGMENT, days_old=10)

    run_council(cfg, sample=5, min_age_days=7)

    repo = ProvenanceEdgeRepository(cfg)
    assert repo.edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_REHABILITATES) == []
    assert repo.edges_from(OBJECT_CYCLE, cycle_id, relation_type=REL_PROMOTED_BY) == []
    assert repo.edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_RETIRES) == []
