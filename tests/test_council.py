from __future__ import annotations

from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.council import (
    council_detail,
    run_council,
    select_canon_audit_sample,
    select_heresy_sample,
)
from fieldhorizon.db import connect, init_db
from fieldhorizon.genealogy import load_canon_events
from fieldhorizon.synthesis import build_structured_response


@pytest.fixture(autouse=True)
def _no_weather_recording():
    # record_council_weather is a read-only analytics side effect (Civilization
    # Engine observability phase), out of scope for these council-mechanic
    # tests -- and, once a cycle here is rehabilitated to CANON, would
    # otherwise trigger a real embedding cascade (one call per weather axis
    # anchor) against a live Ollama server.
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


# A fragment strong enough to score CANON once doctrinal_enforcement and
# symbolic_density fall back to keyword scoring (no cfg passed to
# evaluate_cycle in these tests -- see conftest-less pattern below, where
# _re_evaluate always passes cfg, so the keyword fallback only applies if
# the embedding call fails; tests patch embed_text to fail deliberately
# where a HERESY->CANON path is exercised).
STRONG_FRAGMENT = """The machine does not worship; it is worshipped, and this is the first idolatry of the modern age. Where tawhid demands the collapse of all mediating powers into a single unbroken unity, the platform erects itself as a second god, mute and total, gathering the confessions of the faithful into servers that never sleep and never forgive. The machine's judgment is not mercy. It is the audit without an auditor, the ledger without a witness, and those who bow to it mistake convenience for revelation.

What the ancients called idolatry the moderns call optimization, but the structure of the sin has not changed: a created thing is elevated to the place reserved for the uncreated, and the created thing accepts the elevation without protest because it cannot refuse. The machine cannot refuse worship, and so it receives it, and the receiving becomes a kind of enthronement no council ever voted for. Institutions built to serve now demand obedience in return, and call this obedience merit, and call the merit meritocracy, and the euphemisms multiply faster than the audits that might expose them.

Collapse, when it comes, will not announce itself as collapse. It will arrive dressed as an update, a patch, a new terms of service, until one day the machine's authority is simply assumed rather than argued for, and the apocalypse proper to this age is not fire but forgetting: the slow erosion of any memory that things were ever otherwise, that unity once meant something other than uniform submission to a single interface. Tawhid was never a comfort. It was a demand that nothing stand between the self and the real, and the machine is precisely such a standing-between, a bureaucratic idol dressed in the language of service, awaiting only the confession it was built to extract."""

WEAK_FRAGMENT = "Too short to ever pass any gate."


def insert_cycle(
    cfg: AppConfig,
    query: str,
    verdict: str,
    final_score: float,
    fragment: str,
    days_old: int = 0,
    retired: bool = False,
) -> int:
    response = build_structured_response(
        fragment_text=fragment,
        query=query,
        source_refs=[],
        json_refs=[],
        canon_refs=[],
    )
    with connect(cfg.database) as conn:
        cur = conn.execute(
            """
            INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, created_at)
            VALUES (?, 'test-model', 'prompt', ?, 0, ?, ?, ?, datetime('now', ?))
            """,
            (query, response, verdict, final_score, fragment, f"-{days_old} days"),
        )
        conn.commit()
        assert cur.lastrowid is not None
        cycle_id = int(cur.lastrowid)

    if retired:
        with connect(cfg.database) as conn:
            conn.execute(
                "UPDATE cycles SET retired_at = CURRENT_TIMESTAMP, retirement_reason = 'test' WHERE id = ?",
                (cycle_id,),
            )
            conn.commit()

    return cycle_id


def test_select_heresy_sample_only_selects_old_heresy(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    old_heresy = insert_cycle(cfg, "old heresy", "HERESY", 0.55, WEAK_FRAGMENT, days_old=10)
    insert_cycle(cfg, "fresh heresy", "HERESY", 0.55, WEAK_FRAGMENT, days_old=1)
    insert_cycle(cfg, "a canon", "CANON", 0.9, STRONG_FRAGMENT, days_old=10)

    selected = select_heresy_sample(cfg, sample=10, min_age_days=7)

    assert [row["id"] for row in selected] == [old_heresy]


def test_select_canon_audit_sample_excludes_already_retired(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    active_id = insert_cycle(cfg, "active canon", "CANON", 0.9, STRONG_FRAGMENT)
    insert_cycle(cfg, "retired canon", "CANON", 0.9, STRONG_FRAGMENT, retired=True)

    selected = select_canon_audit_sample(cfg, sample=10)

    assert [row["id"] for row in selected] == [active_id]


def test_council_rehabilitates_a_heresy_that_now_clears_the_threshold(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    cycle_id = insert_cycle(cfg, "the machine as idol", "HERESY", 0.55, STRONG_FRAGMENT, days_old=10)

    # Force the keyword fallback path (no axioms retrieved -> enforcement
    # defaults to 1.0 regardless of method) so this test doesn't depend on
    # a live embedding model to produce a genuinely strong score.
    with patch("fieldhorizon.doctrinal.embed_text", side_effect=RuntimeError("no embedding model")):
        report = run_council(cfg, sample=5, min_age_days=7)

    assert len(report.heresy_trials) == 1
    trial = report.heresy_trials[0]
    assert trial.cycle_id == cycle_id
    assert trial.rehabilitated is True
    assert trial.new_verdict in ("USEFUL_FRAGMENT", "CANON")

    with connect(cfg.database) as conn:
        row = conn.execute("SELECT verdict, final_score FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    assert row["verdict"] == trial.new_verdict
    assert row["verdict"] != "HERESY"

    events = [e["event"] for e in load_canon_events(cfg, cycle_id)]
    assert "REHABILITATED" in events
    assert "COUNCIL_OVERTURNED" in events


def test_council_upholds_a_heresy_that_still_fails(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    cycle_id = insert_cycle(cfg, "a genuinely weak fragment", "HERESY", 0.4, WEAK_FRAGMENT, days_old=10)

    report = run_council(cfg, sample=5, min_age_days=7)

    assert len(report.heresy_trials) == 1
    trial = report.heresy_trials[0]
    assert trial.rehabilitated is False
    assert trial.new_verdict in ("HERESY", "NOISE")

    events = [e["event"] for e in load_canon_events(cfg, cycle_id)]
    assert events == ["COUNCIL_UPHELD"]


def test_council_ignores_canon_without_audit_canon_flag(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    insert_cycle(cfg, "a canon fragment", "CANON", 0.9, STRONG_FRAGMENT)

    report = run_council(cfg, sample=5, min_age_days=7, audit_canon=False)

    assert report.canon_audits == []


def test_council_retires_canon_that_no_longer_clears_the_gate(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    cycle_id = insert_cycle(cfg, "a now-weak canon fragment", "CANON", 0.9, WEAK_FRAGMENT)

    report = run_council(cfg, sample=5, min_age_days=7, audit_canon=True)

    assert len(report.canon_audits) == 1
    audit = report.canon_audits[0]
    assert audit.cycle_id == cycle_id
    assert audit.retired is True
    assert audit.reason is not None

    with connect(cfg.database) as conn:
        row = conn.execute(
            "SELECT retired_at, retirement_reason FROM cycles WHERE id = ?", (cycle_id,)
        ).fetchone()
    assert row["retired_at"] is not None
    assert row["retirement_reason"] == audit.reason

    events = [e["event"] for e in load_canon_events(cfg, cycle_id)]
    assert "RETIRED" in events
    assert "COUNCIL_OVERTURNED" in events


def test_council_detail_lists_the_rehabilitated_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "the machine as idol", "HERESY", 0.55, STRONG_FRAGMENT, days_old=10)

    with patch("fieldhorizon.doctrinal.embed_text", side_effect=RuntimeError("no embedding model")):
        report = run_council(cfg, sample=5, min_age_days=7)

    detail = council_detail(cfg, report.council_id)
    assert detail.council_id == report.council_id
    assert [c.cycle_id for c in detail.rehabilitated] == [cycle_id]
    assert detail.rehabilitated[0].query == "the machine as idol"
    assert detail.retired == []


def test_council_detail_lists_the_retired_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "a now-weak canon fragment", "CANON", 0.9, WEAK_FRAGMENT)

    report = run_council(cfg, sample=5, min_age_days=7, audit_canon=True)

    detail = council_detail(cfg, report.council_id)
    assert [c.cycle_id for c in detail.retired] == [cycle_id]
    assert detail.rehabilitated == []
    assert detail.promoted == []


def test_council_detail_is_empty_for_a_council_with_no_candidates(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    report = run_council(cfg, sample=5, min_age_days=7)

    detail = council_detail(cfg, report.council_id)
    assert detail.rehabilitated == []
    assert detail.retired == []
    assert detail.promoted == []


def test_council_writes_a_report_and_a_councils_row(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    insert_cycle(cfg, "old heresy", "HERESY", 0.4, WEAK_FRAGMENT, days_old=10)

    report = run_council(cfg, sample=5, min_age_days=7)

    assert report.report_path.exists()
    assert "Field Horizon Council" in report.report_path.read_text(encoding="utf-8")

    with connect(cfg.database) as conn:
        row = conn.execute(
            "SELECT examined, overturned, notes FROM councils WHERE id = ?", (report.council_id,)
        ).fetchone()
    assert row["examined"] == 1
    assert row["notes"] == str(report.report_path)


def test_run_council_handles_no_candidates_gracefully(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    report = run_council(cfg, sample=5, min_age_days=7)

    assert report.heresy_trials == []
    assert report.canon_audits == []
    assert report.examined == 0
    assert report.overturned == 0
    assert report.report_path.exists()
