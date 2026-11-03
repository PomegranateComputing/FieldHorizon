"""
fieldhorizon.dbeng units that need no database: statistics, policy (authority,
candidate eligibility, acceptance), advisor, targets, agents.
"""
from __future__ import annotations

import pytest

from fieldhorizon.dbeng import agents as ag
from fieldhorizon.dbeng.adapters.postgres import explain_result_from_json, parse_filter
from fieldhorizon.dbeng.advisor import parse_candidate_argument, propose_index_candidates, structural_findings
from fieldhorizon.dbeng.model import IndexSpec
from fieldhorizon.dbeng.policy import (
    FAIL,
    PASS,
    RETAIN,
    REVERT,
    UNVERIFIED,
    authorize_write,
    decide,
    equality_selectivity,
    evaluate_candidate,
    load_policy,
)
from fieldhorizon.dbeng.stats import bootstrap_median_ratio_ci, mann_whitney_less
from fieldhorizon.dbeng.targets import TargetConfig, TargetConfigError, load_targets, redact

POLICY = load_policy()


def target(**overrides) -> TargetConfig:
    base = dict(
        name="t", engine="postgresql", host="127.0.0.1", port=55432, dbname="db", observer_user="obs",
        operator_user="op", environment="isolated_test", instance_marker="m-1", system_identifier="42",
    )
    return TargetConfig(**{**base, **overrides})


IDENTITY = {"in_recovery": False, "instance_marker": "m-1", "system_identifier": "42"}


# --- statistics -----------------------------------------------------------------
def test_rank_test_separates_clearly_different_samples_and_not_identical_ones():
    fast, slow = [1.0 + i * 0.01 for i in range(20)], [5.0 + i * 0.01 for i in range(20)]
    assert mann_whitney_less(fast, slow) < 1e-6
    assert mann_whitney_less(slow, fast) > 0.99
    same = [2.0] * 20
    assert 0.3 < mann_whitney_less(same, same) <= 1.0


def test_bootstrap_interval_is_deterministic():
    before, after = [10.0, 11.0, 9.5, 10.2, 10.8], [5.0, 5.2, 4.9, 5.1, 5.3]
    assert bootstrap_median_ratio_ci(before, after) == bootstrap_median_ratio_ci(before, after)


# --- authority ---------------------------------------------------------------------
def test_writes_are_authorized_only_on_the_proven_isolated_target():
    assert authorize_write(target(), IDENTITY, POLICY, "create_index_concurrently").eligible


@pytest.mark.parametrize("change, failing", [
    ({"environment": "production"}, "environment_permits_writes"),
    ({"instance_marker": "other"}, "instance_marker_matches"),
    ({"system_identifier": "999"}, "system_identifier_matches"),
    ({"operator_user": None}, "operator_identity_declared"),
])
def test_writes_are_refused_off_the_isolated_target(change, failing):
    verdict = authorize_write(target(**change), IDENTITY, POLICY, "create_index_concurrently")
    assert not verdict.eligible
    assert failing in [c.name for c in verdict.blocking]


def test_an_unprovable_identity_blocks_writes():
    verdict = authorize_write(target(), {**IDENTITY, "system_identifier": None}, POLICY, "create_index_concurrently")
    assert [c.status for c in verdict.blocking] == [UNVERIFIED]


def test_a_replica_and_an_unlisted_operation_are_refused():
    assert not authorize_write(target(), {**IDENTITY, "in_recovery": True}, POLICY, "create_index_concurrently").eligible
    assert not authorize_write(target(), IDENTITY, POLICY, "drop_table").eligible


# --- candidate eligibility ------------------------------------------------------------
def facts(**overrides) -> dict:
    base = {
        "exists": True, "kind": "table", "partitioning": "available", "estimated_rows": 2_000_000, "owner": "own",
        "columns": ["id", "customer_id", "status", "note"], "index_count": 1,
        "indexes": [{"index": "orders_pkey", "is_valid": True, "has_expressions": False, "is_partial": False,
                     "method": "btree", "columns": ["id"]}],
        "dependency_status": "available", "unverified_dependencies": [], "operator_can_create_index": True,
    }
    return {**base, **overrides}


STATS = {"customer_id": {"n_distinct": 190_000.0, "mcf": [], "avg_width": 4}, "status": {"n_distinct": 5, "mcf": [0.2, 0.2], "avg_width": 7}}


def verdict(spec_text="bench.orders(customer_id)", f=None, stats=None, running=None, free=10**12):
    cand = parse_candidate_argument(spec_text)
    return evaluate_candidate(cand, f or facts(), STATS if stats is None else stats, running or [], POLICY, free)


def test_a_selective_unindexed_column_is_eligible():
    v = verdict()
    assert v.eligible, v.describe()


@pytest.mark.parametrize("spec_text, f, stats, running, free, failing", [
    ("bench.audit_log(event_type)", None, None, None, 10**12, "table_not_protected"),
    ("pg_catalog.pg_class(relname)", None, None, None, 10**12, "schema_allowed"),
    ("bench.orders(status)", None, None, None, 10**12, "leading_column_selective"),
    ("bench.orders(id)", None, None, None, 10**12, "not_already_indexed"),
    ("bench.orders(missing)", None, None, None, 10**12, "columns_exist"),
    ("bench.orders(customer_id)", facts(kind="partitioned_table", partitioning="unsupported"), None, None, 10**12, "relation_kind_supported"),
    ("bench.orders(customer_id)", facts(operator_can_create_index=False), None, None, 10**12, "operator_privilege"),
    ("bench.orders(customer_id)", None, None, [{"xact_seconds": 600.0}], 10**12, "no_long_transactions"),
    ("bench.orders(customer_id)", None, None, None, 1000, "storage_available"),
    ("bench.orders(customer_id)", facts(estimated_rows=50), None, None, 10**12, "table_large_enough"),
])
def test_ineligible_candidates_are_refused_with_the_reason(spec_text, f, stats, running, free, failing):
    v = verdict(spec_text, f, stats, running, free)
    assert not v.eligible
    assert failing in [c.name for c in v.blocking]


@pytest.mark.parametrize("f, stats, free, unverified", [
    (None, {}, 10**12, "leading_column_selective"),
    (None, None, None, "storage_available"),
    (facts(dependency_status="unverified", unverified_dependencies=[{"catalog": "pg_foo"}]), None, 10**12, "dependencies_verified"),
])
def test_anything_unverifiable_blocks(f, stats, free, unverified):
    v = verdict(f=f, stats=stats, free=free)
    assert not v.eligible
    assert unverified in [c.name for c in v.blocking if c.status == UNVERIFIED]


def test_equality_selectivity_reads_pg_stats_conventions():
    assert equality_selectivity({"mcf": [0.21, 0.2], "n_distinct": 5}, 100) == 0.21
    assert equality_selectivity({"mcf": [], "n_distinct": 200.0}, 10_000) == 0.005
    assert equality_selectivity({"mcf": None, "n_distinct": -1.0}, 1000) == 0.001
    assert equality_selectivity({}, 1000) is None


# --- acceptance ---------------------------------------------------------------------------
STABLE = [10.0 + 0.1 * (i % 5) for i in range(32)]
CONTROL = [4.0 + 0.01 * (i % 3) for i in range(32)]


def _decide(baseline, after, roles=None, weights=None, usage=None, control_after=None):
    baseline = {**baseline, "control": CONTROL}
    after = {**after, "control": control_after or CONTROL}
    roles = roles or {q: "read" for q in baseline}
    roles = {**roles, "control": "read"}
    weights = weights or {q: 1.0 for q in baseline}
    weights = {**weights, "control": 0.0}
    usage = usage if usage is not None else {"target": 1.0}
    return decide(baseline, after, roles, weights, ["target"], usage, POLICY, controls=["control"])


def test_a_drifting_environment_is_not_credited():
    d = _decide({"target": STABLE}, {"target": [1.0 + 0.01 * (i % 5) for i in range(32)]},
                control_after=[6.0 + 0.01 * (i % 3) for i in range(32)])
    assert d.outcome == REVERT
    assert [c.status for c in d.criteria if c.name == "measurement_stable"] == [FAIL]


def test_without_a_control_query_drift_cannot_be_bounded():
    d = decide({"target": STABLE}, {"target": [1.0] * 32}, {"target": "read"}, {"target": 1.0}, ["target"], {"target": 1.0}, POLICY)
    assert d.outcome == REVERT
    assert [c.detail for c in d.criteria if c.name == "measurement_stable"] == ["the workload has no control query: drift cannot be bounded"]


def test_a_large_consistent_improvement_is_retained():
    d = _decide({"target": STABLE}, {"target": [1.0 + 0.01 * (i % 5) for i in range(32)]})
    assert d.outcome == RETAIN, [c for c in d.criteria if c.status != PASS]


def test_one_lucky_run_is_not_an_improvement():
    lucky = STABLE[:31] + [0.1]
    d = _decide({"target": STABLE}, {"target": lucky})
    assert d.outcome == REVERT
    assert [c.status for c in d.criteria if c.name == "target_improved:target"] == [FAIL]


def test_a_noisy_baseline_cannot_be_credited():
    noisy = [10.0] * 16 + [20.0] * 16
    d = _decide({"target": noisy}, {"target": [1.0] * 32})
    assert d.outcome == REVERT
    assert [c.status for c in d.criteria if c.name == "baseline_noise_bounded"] == [FAIL]


def test_an_unused_index_is_not_credited():
    d = _decide({"target": STABLE}, {"target": [1.0] * 32}, usage={"target": 0.0})
    assert d.outcome == REVERT


def test_a_significant_write_regression_reverts():
    write_before = [5.0 + 0.01 * (i % 3) for i in range(32)]
    write_after = [8.0 + 0.01 * (i % 3) for i in range(32)]
    d = _decide(
        {"target": STABLE, "insert": write_before}, {"target": [1.0] * 32, "insert": write_after},
        roles={"target": "read", "insert": "write"}, weights={"target": 1.0, "insert": 100.0},
    )
    assert d.outcome == REVERT
    failed = {c.name for c in d.criteria if c.status != PASS}
    assert {"no_regression:insert", "weighted_workload_improved"} <= failed


# --- advisor -------------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("(customer_id = 42)", [{"column": "customer_id", "op": "="}]),
    ("(orders.customer_id = 42)", [{"column": "customer_id", "op": "="}]),
    ("((status)::text = 'paid'::text)", [{"column": "status", "op": "="}]),
    ("((orders.customer_id = 7) AND (orders.created_at >= '2025-01-01 00:00:00+00'::timestamp with time zone))",
     [{"column": "customer_id", "op": "="}, {"column": "created_at", "op": ">="}]),
    ("((customer_id = 1) OR (customer_id = 2))", []),
    ("(lower(note) = 'x'::text)", []),
    ("(a.x = a.y)", []),
    ("(status <> 'new'::text)", []),
])
def test_filter_parsing_accepts_only_what_a_plain_btree_serves(text, expected):
    assert parse_filter(text) == expected


def _plan(filter_text, rows, removed):
    return explain_result_from_json([{
        "Plan": {"Node Type": "Seq Scan", "Relation Name": "orders", "Schema": "bench", "Filter": filter_text,
                 "Actual Rows": rows, "Actual Loops": 1, "Rows Removed by Filter": removed},
        "Planning Time": 0.1, "Execution Time": 60.0,
    }])


def test_the_advisor_proposes_from_selective_scans_and_explains_the_rest():
    candidates, observations = propose_index_candidates({
        "by_customer": _plan("(orders.customer_id = 42)", 10, 1_999_990),
        "by_status": _plan("(orders.status = 'paid'::text)", 400_000, 1_600_000),
        "by_or": _plan("((orders.customer_id = 1) OR (orders.customer_id = 2))", 20, 1_999_980),
    })
    assert [c.spec.key for c in candidates] == ["bench.orders(customer_id)"]
    assert candidates[0].target_queries == ["by_customer"]
    outcomes = {o["query_id"]: o["outcome"] for o in observations}
    assert "not selective" in outcomes["by_status"]
    assert "not a plain-column comparison" in outcomes["by_or"]


def test_index_names_are_deterministic_and_fit_postgres_limits():
    spec = IndexSpec("bench", "orders", ("customer_id",))
    assert spec.name == IndexSpec("bench", "orders", ("customer_id",)).name
    long = IndexSpec("s", "t" * 80, ("c" * 80, "d" * 80))
    assert len(long.name) <= 63


def test_structural_findings_flag_an_unindexed_foreign_key():
    snapshot = {
        "tables": [{"schema": "bench", "table": "orders", "kind": "table", "estimated_rows": 10, "column_statistics": "available"}],
        "indexes": [{"schema": "bench", "table": "orders", "index": "orders_pkey", "columns": ["id"], "is_valid": True,
                     "method": "btree", "has_expressions": False, "is_partial": False}],
        "constraints": [
            {"schema": "bench", "table": "orders", "name": "orders_pkey", "type": "p", "columns": ["id"], "references": None},
            {"schema": "bench", "table": "orders", "name": "fk", "type": "f", "columns": ["customer_id"], "references": "bench.customers"},
        ],
        "table_statistics": [],
    }
    kinds = [f["kind"] for f in structural_findings(snapshot)]
    assert kinds == ["foreign_key_without_index"]


# --- targets ---------------------------------------------------------------------------------
def test_a_target_file_with_a_credential_is_refused(tmp_path):
    path = tmp_path / "targets.yaml"
    path.write_text("targets:\n  t:\n    engine: postgresql\n    host: h\n    port: 1\n    dbname: d\n"
                    "    observer_user: o\n    environment: isolated_test\n    password: hunter2\n", encoding="utf-8")
    with pytest.raises(TargetConfigError, match="credential"):
        load_targets(path)


def test_driver_messages_are_redacted():
    assert "hunter2" not in redact("connection failed: host=h password=hunter2 user=u")
    assert "hunter2" not in redact("postgresql://u:hunter2@h:5432/d")


# --- agents ----------------------------------------------------------------------------------
EVIDENCE = {"E1": {"kind": "baseline_plan", "query_id": "q"}, "E2": {"kind": "candidate", "key": "bench.orders(customer_id)"}}


def test_uncited_or_invented_claims_are_discarded():
    reply = ('{"findings": [{"claim": "seq scan on orders", "evidence": ["E1"]}, {"claim": "trust me", "evidence": []},'
             ' {"claim": "made up", "evidence": ["E9"]}]}')
    report = ag.run_role(None, ag.ROLE_DIAGNOSTICIAN, EVIDENCE, "t", model="m", llm=lambda p: reply)  # type: ignore[arg-type]
    assert [a["claim"] for a in report.accepted] == ["seq scan on orders"]
    assert sorted(r["reason"] for r in report.rejected) == ["cites no evidence", "cites unknown evidence ids ['E9']"]


def test_planner_proposals_need_citations_and_reviewer_objections_are_mapped_to_checks():
    planner = ag.run_role(None, ag.ROLE_PLANNER, EVIDENCE, "t", model="m", llm=lambda p: (  # type: ignore[arg-type]
        '{"proposals": [{"schema": "bench", "table": "orders", "columns": ["customer_id"], "evidence": ["E1"]},'
        ' {"schema": "bench", "table": "orders", "columns": ["status"]}]}'))
    assert [p["columns"] for p in planner.proposals] == [["customer_id"]]
    reviewer = ag.run_role(None, ag.ROLE_REVIEWER, EVIDENCE, "t", model="m", llm=lambda p: (  # type: ignore[arg-type]
        '{"objections": [{"candidate_id": "C-1", "type": "low_selectivity", "objection": "x", "evidence": ["E2"]}]}'))
    mapped = ag.map_objections(reviewer.objections, {"C-1": {"checks": [{"name": "leading_column_selective", "status": "pass", "detail": "d"}]}})
    assert mapped[0]["check_results"][0]["name"] == "leading_column_selective"
    assert mapped[0]["effect"].startswith("context only")


def test_an_llm_failure_never_breaks_the_pipeline():
    def boom(prompt):
        raise RuntimeError("ollama down")

    report = ag.run_role(None, ag.ROLE_VALIDATOR, EVIDENCE, "t", model="m", llm=boom)  # type: ignore[arg-type]
    assert report.error == "RuntimeError: ollama down"
    assert report.assessment is None


def test_db_commands_work_on_a_fresh_field_horizon_database(tmp_path, capsys, monkeypatch):
    """Regression: the journal's events need init_db; a brand-new config used to fail on the first event."""
    import json as _json

    from fieldhorizon.cli import main

    (tmp_path / "config.yaml").write_text("paths:\n  database: fresh.sqlite3\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["field-horizon", "--config", str(tmp_path / "config.yaml"), "db", "operations", "--json"])
    main()
    assert _json.loads(capsys.readouterr().out) == []
    import sqlite3

    with sqlite3.connect(tmp_path / "fresh.sqlite3") as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"domain_events", "provenance_edges", "dbeng_operations"} <= tables


def test_bare_evidence_ids_are_quoted_but_must_still_exist():
    """Seen with hermes3:8b on the real bench: `"evidence": [E25]` (invalid JSON, valid intent)."""
    assert ag.quote_bare_evidence_ids('{"evidence": [E1, "E2" ,E9]}') == '{"evidence": ["E1", "E2" ,"E9"]}'
    assert ag.quote_bare_evidence_ids('{"claim": "E1 is slow", "evidence": []}') == '{"claim": "E1 is slow", "evidence": []}'
    reply = '{"assessment": "retain", "findings": [{"claim": "kept", "evidence": [E1]}, {"claim": "x", "evidence": [E9]}]}'
    report = ag.run_role(None, ag.ROLE_VALIDATOR, EVIDENCE, "t", model="m", llm=lambda p: reply)  # type: ignore[arg-type]
    assert report.normalised and report.assessment == "retain"
    assert [a["claim"] for a in report.accepted] == ["kept"]
    assert report.rejected[0]["reason"] == "cites unknown evidence ids ['E9']"
