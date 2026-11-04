"""
The autonomous operation against a deterministic in-memory target (tests/dbeng_fakes.py): every terminal
path, the journal, events, evidence, the target lease, operator controls, re-verification before the
change, compensation, and recovery in its three modes. The same paths run against a real PostgreSQL in
test_dbeng_postgres.py.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from dbeng_fakes import FakeAdapter, FakeServer, executor_factory, make_config, make_target, make_workload

from fieldhorizon.db import connect, init_db
from fieldhorizon.dbeng import recovery
from fieldhorizon.dbeng.export import recompute_decision, verification
from fieldhorizon.dbeng.journal import Journal
from fieldhorizon.dbeng.model import IndexState
from fieldhorizon.dbeng.operation import IndexOptimization, OptimizeRequest
from fieldhorizon.dbeng.policy import load_policy
from fieldhorizon.events import EventRepository

POLICY = load_policy()
STEPS_RETAINED = ("preflight", "discover", "baseline", "diagnose", "validate", "authorize", "apply", "measure", "decide",
                  "retain", "verify")


class SimulatedCrash(BaseException):
    """Escapes the operation's `except Exception` exactly like a killed process: no cleanup runs."""


@pytest.fixture
def cfg(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    return cfg


def run(cfg, adapter, *, policy=None, workload=None, candidates=None, llm=None, fault_after=None, fault_hook=None,
        factory=None, test_hooks=None, operation_id=None, sleep=lambda s: None):
    policy = policy or POLICY
    target = adapter.target
    request = OptimizeRequest(target=target.name, workload_path="memory", candidates=candidates or [],
                              use_llm=llm is not None, fault_after=fault_after)
    factory = factory or executor_factory(target, policy, adapter.server)
    return IndexOptimization(cfg, target, adapter, policy, workload or make_workload(), request, llm=llm, fault_hook=fault_hook,
                             executor_factory=factory, test_hooks=test_hooks, operation_id=operation_id, sleep=sleep).run()


def kinds(adapter) -> list[str]:
    return [c.split(":")[0] for c in adapter.calls if not c.startswith("acquire")]


def test_a_justified_index_is_built_measured_and_retained_without_approval(cfg):
    adapter = FakeAdapter(make_target())
    outcome = run(cfg, adapter)

    assert outcome.status == "RETAINED", outcome.message
    assert outcome.cause == "acceptance_passed"
    assert kinds(adapter) == ["create", "analyze"]
    assert len(adapter.indexes) == 1
    journal = Journal(cfg)
    assert [(s["step"], s["status"]) for s in journal.steps(outcome.operation_id)] == [(s, "completed") for s in STEPS_RETAINED]
    assert [c["state"] for c in journal.changes(outcome.operation_id)] == ["retained"]
    assert [r["state"] for r in adapter.server.ledger_rows()] == ["retained"]
    assert adapter.server.lock_owner is None, "the target lease is released at the end"
    for artifact in journal.artifacts(outcome.operation_id):
        assert hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest() == artifact["sha256"]
    names = {a["name"] for a in journal.artifacts(outcome.operation_id)}
    assert {"policy.yaml", "workload.yaml", "authorization.json", "decision.json", "report.md"} <= names
    events = [e.event_type for e in EventRepository(cfg).by_correlation(outcome.operation_id)]
    assert events[0] == "DbOperationStarted" and events[-1] == "DbOperationCompleted"
    assert "DbChangeApplied" in events and "DbDecisionRecorded" in events
    with connect(cfg.database) as conn:
        assert conn.execute("SELECT count(*) FROM provenance_edges WHERE provenance_ref = ?", (outcome.operation_id,)).fetchone()[0] >= 2
    decision = outcome.decision
    assert decision and "measurement_stable" in {c["name"] for c in decision["criteria"]}
    assert decision["queries"]["lookup"]["client"]["median_ratio"] < 0.1, "client latency is reported beside server time"
    assert decision["side_effects"]["baseline"]["tables"]["bench.orders"]["n_dead_tup"] > 0, "rolled-back writes leave traces"


def test_rerunning_after_a_retained_change_is_a_no_op(cfg):
    adapter = FakeAdapter(make_target())
    assert run(cfg, adapter).status == "RETAINED"
    again = run(cfg, adapter)
    assert again.status == "NO_CANDIDATE"
    assert kinds(adapter) == ["create", "analyze"]


def test_a_measured_regression_is_compensated_and_confirmed(cfg):
    adapter = FakeAdapter(make_target(), write_penalty=1.0)
    outcome = run(cfg, adapter, workload=make_workload(write_weight=1000.0))
    assert outcome.status == "REVERTED" and outcome.cause == "acceptance_failed"
    assert adapter.indexes == {}
    assert [r["state"] for r in adapter.server.ledger_rows()] == ["reverted"]
    failed = {c["name"] for c in outcome.decision["criteria"] if c["status"] != "pass"}
    assert "weighted_workload_improved" in failed
    compensation = json.loads(Path(outcome.evidence_dir, "compensation.json").read_text(encoding="utf-8"))
    assert compensation["drop"]["dropped"] and compensation["drop"]["proof"]["proven"]
    assert compensation["post_check"]["catalog"]["index_absent"] is True
    assert compensation["post_check"]["catalog"]["plans_use_removed_index"] is False


def test_environment_drift_is_not_credited_to_the_change(cfg):
    adapter = FakeAdapter(make_target(), control_drift=0.5)
    outcome = run(cfg, adapter)
    assert outcome.status == "REVERTED"
    failed = {c["name"]: c["detail"] for c in outcome.decision["criteria"] if c["status"] != "pass"}
    assert "measurement_stable" in failed and "moved beyond" in failed["measurement_stable"]


@pytest.mark.parametrize("target_change", [{"environment": "production"}, {"instance_marker": "someone-else"}])
def test_no_write_and_no_lease_without_authority(cfg, target_change):
    adapter = FakeAdapter(make_target(**target_change))
    outcome = run(cfg, adapter)
    assert outcome.status == "BLOCKED" and outcome.cause == "authority_refused"
    assert adapter.calls == []


def test_poor_and_prohibited_candidates_are_rejected_without_touching_the_database(cfg):
    adapter = FakeAdapter(make_target())
    workload = replace(make_workload(), queries=tuple(q for q in make_workload().queries if q.query_id in ("summary", "control")))
    outcome = run(cfg, adapter, workload=workload, candidates=["bench.orders(status)", "bench.audit_log(event_type)"])
    assert outcome.status == "REJECTED"
    assert kinds(adapter) == []
    verdicts = json.loads(Path(outcome.evidence_dir, "policy_verdicts.json").read_text(encoding="utf-8"))["verdicts"]
    blocking = sorted(b for v in verdicts.values() for b in v["blocking"])
    assert blocking == ["leading_column_selective", "leading_column_selective", "operator_privilege", "table_not_protected"]


def test_control_queries_are_never_diagnosed_for_candidates(cfg):
    adapter = FakeAdapter(make_target())
    outcome = run(cfg, adapter)
    candidates = json.loads(Path(outcome.evidence_dir, "candidates.json").read_text(encoding="utf-8"))
    assert candidates["controls_excluded"] == ["control"]
    assert all("control" not in c["target_queries"] for c in candidates["candidates"])


def test_a_second_operation_on_a_held_target_is_blocked_by_the_lease(cfg):
    server = FakeServer()
    adapter = FakeAdapter(make_target(), server)
    other = executor_factory(make_target(), POLICY, server)("OP-other-process")
    other.acquire()
    outcome = run(cfg, adapter)
    assert outcome.status == "BLOCKED" and outcome.cause == "lease_busy"
    assert kinds(adapter) == []
    other.release()
    assert run(cfg, adapter).status == "RETAINED"


def test_a_failure_after_the_change_undoes_it_and_says_so(cfg):
    adapter = FakeAdapter(make_target(), fail_on_explain_after_create=True)
    outcome = run(cfg, adapter)
    assert outcome.status == "FAILED_RECOVERED"
    assert adapter.indexes == {}
    assert "connection lost" in outcome.message
    assert [r["state"] for r in adapter.server.ledger_rows()] == ["reverted"]


def test_a_lock_timeout_during_the_build_is_cleaned_up_and_reported(cfg):
    adapter = FakeAdapter(make_target())
    outcome = run(cfg, adapter, factory=executor_factory(make_target(), POLICY, adapter.server, fail_create="ddl_lock_timeout"))
    assert outcome.status == "FAILED_RECOVERED" and outcome.cause == "ddl_lock_timeout"
    assert adapter.indexes == {}


def test_a_build_left_invalid_and_not_cleaned_needs_recovery_then_is_recovered(cfg):
    adapter = FakeAdapter(make_target())
    adapter.server.drops_blocked = True  # the transaction that made the build time out still holds its lock
    outcome = run(cfg, adapter, factory=executor_factory(make_target(), POLICY, adapter.server, fail_create="ddl_lock_timeout",
                                                          cleanup_fails=True))
    assert outcome.status == "FAILED_NEEDS_RECOVERY" and outcome.cause == "ddl_lock_timeout"
    [leftover] = adapter.indexes.values()
    assert not leftover.valid
    adapter.server.drops_blocked = False  # the transaction ended
    report = recovery.recover(cfg, make_target(), adapter, POLICY, executor_factory(make_target(), POLICY, adapter.server),
                              mode=recovery.MODE_MANUAL, sleep=lambda s: None)
    assert report["status"] == "RECOVERY_DONE"
    assert adapter.indexes == {}
    record = Journal(cfg).get(outcome.operation_id)
    assert record is not None and record.status == "FAILED_RECOVERED"


def test_a_crash_after_apply_is_recovered_automatically_before_the_next_change(cfg):
    adapter = FakeAdapter(make_target())
    factory = executor_factory(make_target(), POLICY, adapter.server)

    def crash(step):
        factory.made[-1].crash()  # the process dies: the server ends its session and releases the lock
        raise SimulatedCrash(step)

    with pytest.raises(SimulatedCrash):
        run(cfg, adapter, fault_after="apply", fault_hook=crash, factory=factory)
    journal = Journal(cfg)
    [interrupted] = journal.unfinished()
    assert interrupted.state == "apply" and len(adapter.indexes) == 1

    detection = recovery.detect(adapter, journal, make_target(), POLICY)
    assert not detection["clean"] and detection["recoverable"]
    assert {i["kind"] for i in detection["incidents"]} >= {"ledger_unfinished", "journal_unfinished"}

    outcome = run(cfg, adapter)  # policy v2: recovery.automatic = true
    assert outcome.status == "RETAINED"
    recovered = journal.get(interrupted.operation_id)
    assert recovered is not None and recovered.status == "RECOVERED_REVERTED"
    assert (journal.runtime(interrupted.operation_id) or {})["recovery_mode"] == "automatic_preflight"
    _, runs = journal.query(kind="recovery")
    assert runs[0].status == "RECOVERY_DONE"


def test_without_automatic_recovery_the_next_operation_blocks_and_manual_recovery_is_idempotent(cfg, tmp_path):
    manual_policy = replace(POLICY, recovery_automatic=False)
    adapter = FakeAdapter(make_target())
    factory = executor_factory(make_target(), manual_policy, adapter.server)

    def crash(step):
        factory.made[-1].crash()
        raise SimulatedCrash(step)

    with pytest.raises(SimulatedCrash):
        run(cfg, adapter, policy=manual_policy, fault_after="measure", fault_hook=crash, factory=factory)
    blocked = run(cfg, adapter, policy=manual_policy)
    assert blocked.status == "BLOCKED" and blocked.cause == "unrecovered_state"
    assert "automatic recovery is disabled" in blocked.message and len(adapter.indexes) == 1

    first = recovery.recover(cfg, make_target(), adapter, manual_policy, executor_factory(make_target(), manual_policy, adapter.server),
                             mode=recovery.MODE_MANUAL, sleep=lambda s: None)
    assert first["status"] == "RECOVERY_DONE" and adapter.indexes == {}
    assert first["actions"][0]["proof"]["basis"] == "ledger + marker"
    second = recovery.recover(cfg, make_target(), adapter, manual_policy, executor_factory(make_target(), manual_policy, adapter.server),
                              mode=recovery.MODE_MANUAL, sleep=lambda s: None)
    assert second["status"] == "RECOVERY_NOTHING_TO_DO" and second["actions"] == []
    assert run(cfg, adapter, policy=manual_policy).status == "RETAINED"


def test_a_build_completed_after_the_client_died_is_proven_by_the_ledger_and_removed(cfg):
    """The Windows case: no marker (the client died before COMMENT), the ledger says 'applying', the structure matches."""
    server = FakeServer()
    adapter = FakeAdapter(make_target(), server)
    server.ledger[("OP-dead", "CH-1")] = {"operation_id": "OP-dead", "change_id": "CH-1", "fencing_token": 1, "index_schema": "bench",
                                          "index_name": "fhx_orders_customer_id_x", "table_name": "orders", "columns": ["customer_id"],
                                          "state": "applying", "index_oid": None, "note": None, "recovered_by": None, "holder_host": "h"}
    server.indexes["fhx_orders_customer_id_x"] = IndexState("bench", "fhx_orders_customer_id_x", "orders", True, True, "CREATE INDEX ...",
                                                           None, 1, oid=777, method="btree", columns=("customer_id",))
    report = recovery.recover(cfg, make_target(), adapter, POLICY, executor_factory(make_target(), POLICY, server),
                              mode=recovery.MODE_AUTOMATIC_STARTUP, sleep=lambda s: None)
    assert report["status"] == "RECOVERY_DONE"
    assert report["actions"][0]["proof"]["basis"].startswith("ledger shows an interrupted build")
    assert server.indexes == {}


def test_recovery_never_drops_what_it_cannot_prove(cfg):
    server = FakeServer()
    adapter = FakeAdapter(make_target(), server)
    # Unledgered, named like ours: reported, never touched.
    server.indexes["fhx_orders_somebody"] = IndexState("bench", "fhx_orders_somebody", "orders", True, True, "CREATE INDEX ...", None, 1,
                                                      oid=5, method="btree", columns=("status",))
    # Ledgered, but the live index was recreated by someone else (OID differs) and its columns differ.
    server.ledger[("OP-x", "CH-1")] = {"operation_id": "OP-x", "change_id": "CH-1", "fencing_token": 1, "index_schema": "bench",
                                       "index_name": "fhx_orders_note_y", "table_name": "orders", "columns": ["note"], "state": "applied",
                                       "index_oid": 100, "note": None, "recovered_by": None, "holder_host": "h"}
    server.indexes["fhx_orders_note_y"] = IndexState("bench", "fhx_orders_note_y", "orders", True, True, "CREATE INDEX ...",
                                                    {"fieldhorizon": "dbeng", "operation_id": "OP-x", "change_id": "CH-1"}, 1,
                                                    oid=101, method="btree", columns=("status",))
    report = recovery.recover(cfg, make_target(), adapter, POLICY, executor_factory(make_target(), POLICY, server),
                              mode=recovery.MODE_MANUAL, sleep=lambda s: None)
    assert report["status"] == "RECOVERY_BLOCKED"
    assert set(server.indexes) == {"fhx_orders_somebody", "fhx_orders_note_y"}
    [blocked] = report["blocked"]
    assert blocked["code"] == "ownership_unproven" and "OID" in blocked["detail"]
    kinds_seen = {i["kind"] for i in report["detection"]["incidents"]}
    assert "unledgered_index" in kinds_seen


def test_orphaned_server_activity_blocks_recovery_after_a_bounded_wait(cfg):
    server = FakeServer()
    adapter = FakeAdapter(make_target(), server)
    server.activity.append({"pid": 99, "application_name": "fh-dbeng/operator/OP-dead", "operation_id": "OP-dead", "identity": "operator",
                            "state": "active", "query_seconds": 30, "query": 'CREATE INDEX CONCURRENTLY "fhx_..."'})
    server.ledger[("OP-dead", "CH-1")] = {"operation_id": "OP-dead", "change_id": "CH-1", "fencing_token": 1, "index_schema": "bench",
                                          "index_name": "fhx_orders_customer_id_z", "table_name": "orders", "columns": ["customer_id"],
                                          "state": "applying", "index_oid": None, "note": None, "recovered_by": None, "holder_host": "h"}
    fast = replace(POLICY, orphan_wait_seconds=0)
    report = recovery.recover(cfg, make_target(), adapter, fast, executor_factory(make_target(), fast, server), sleep=lambda s: None)
    assert report["status"] == "RECOVERY_BLOCKED" and report["actions"] == []
    assert report["blocked"][0]["incident"]["kind"] == "orphan_activity"
    server.activity.clear()  # the server finished the statement and the backend exited
    report = recovery.recover(cfg, make_target(), adapter, fast, executor_factory(make_target(), fast, server), sleep=lambda s: None)
    assert report["status"] == "RECOVERY_DONE"


def test_cancel_before_the_change_changes_nothing(cfg):
    adapter = FakeAdapter(make_target())
    journal = Journal(cfg)
    op = "OP-cancel-early"
    outcome = run(cfg, adapter, operation_id=op, test_hooks={"before_authorize": lambda: journal.request_control(op, "cancel", "test")})
    assert outcome.status == "CANCELLED" and outcome.cause == "cancelled_by_operator"
    assert kinds(adapter) == [] and adapter.server.lock_owner is None
    assert journal.controls(op)[0]["effect"].startswith("cancelling at before authorize")


def test_cancel_after_the_change_removes_it(cfg):
    adapter = FakeAdapter(make_target())
    journal = Journal(cfg)
    op = "OP-cancel-late"
    # The cancel arrives once the index is built; the next safe point (before measuring) compensates.
    outcome = run(cfg, adapter, operation_id=op, fault_after="apply",
                  fault_hook=lambda step: journal.request_control(op, "cancel", "test"))
    assert outcome.status == "CANCELLED" and "removed" in outcome.message
    assert adapter.indexes == {} and [r["state"] for r in adapter.server.ledger_rows()] == ["reverted"]


def test_pause_holds_the_lease_and_resume_continues(cfg):
    adapter = FakeAdapter(make_target())
    journal = Journal(cfg)
    op = "OP-pause"
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            record = journal.get(op)
            assert record is not None and record.status == "PAUSED"
            assert adapter.server.lock_owner is not None, "a paused operation keeps the target reserved"
            journal.request_control(op, "resume", "test")

    outcome = run(cfg, adapter, operation_id=op, sleep=sleep,
                  test_hooks={"before_authorize": lambda: journal.request_control(op, "pause", "test")})
    assert outcome.status == "RETAINED"
    effects = [c["effect"] for c in journal.controls(op)]
    assert effects[0].startswith("paused at") and effects[1].startswith("resumed at")
    events = [e.event_type for e in EventRepository(cfg).by_correlation(op)]
    assert "DbOperationPaused" in events and "DbOperationResumed" in events


def test_a_policy_change_mid_operation_blocks_the_change(cfg, tmp_path):
    policy_file = tmp_path / "policy.yaml"
    shutil.copyfile(POLICY.source, policy_file)
    policy = load_policy(policy_file)
    adapter = FakeAdapter(make_target())

    def edit_policy():
        policy_file.write_bytes(policy_file.read_bytes() + b"\n# edited during the operation\n")

    outcome = run(cfg, adapter, policy=policy, test_hooks={"before_authorize": edit_policy})
    assert outcome.status == "BLOCKED" and outcome.cause == "policy_changed"
    assert kinds(adapter) == []


def test_a_policy_change_between_authorization_and_execution_is_refused_by_the_executor(cfg, tmp_path):
    policy_file = tmp_path / "policy.yaml"
    shutil.copyfile(POLICY.source, policy_file)
    policy = load_policy(policy_file)
    adapter = FakeAdapter(make_target())

    def edit_policy():
        policy_file.write_bytes(policy_file.read_bytes() + b"\n# edited between authorize and apply\n")

    outcome = run(cfg, adapter, policy=policy, test_hooks={"before_apply": edit_policy})
    assert outcome.status == "BLOCKED" and outcome.cause == "policy_changed"
    assert kinds(adapter) == [] and adapter.indexes == {}


def test_a_stale_authorization_is_refused_by_the_executor(cfg):
    adapter = FakeAdapter(make_target())
    now = [1000.0]
    factory = executor_factory(make_target(), POLICY, adapter.server, clock=lambda: now[0])

    def age():
        now[0] += POLICY.max_authorization_age_seconds + 1

    outcome = run(cfg, adapter, factory=factory, test_hooks={"before_apply": age})
    assert outcome.status == "BLOCKED" and outcome.cause == "authorization_stale"
    assert kinds(adapter) == []


def test_llm_agents_inform_but_never_decide_and_their_claims_are_checked(cfg):
    replies = {
        "change planner": '{"proposals": [{"schema": "bench", "table": "audit_log", "columns": ["event_type"], "evidence": ["E1"]}]}',
        "adversarial reviewer": '{"objections": [{"candidate_id": "x", "type": "write_overhead", "objection": "o", "evidence": ["E1"]}]}',
        "independent validator": '{"assessment": "revert", "findings": [{"claim": "c", "evidence": ["E1"]}]}',
        "performance diagnostician": '{"findings": [{"claim": "orders_by_customer does a sequential scan of bench.orders on customer_id, '
                                     'which has no index", "evidence": ["E5"]}, {"claim": "bench.ghosts is slow", "evidence": ["E1"]}]}',
    }

    def llm(prompt: str) -> str:
        for role, reply in replies.items():
            if f"Role: {role}" in prompt:
                return reply
        return '{"findings": [{"claim": "uncited"}]}'

    adapter = FakeAdapter(make_target())
    outcome = run(cfg, adapter, llm=llm)
    assert outcome.status == "RETAINED"  # the validator said "revert"; the measurements decide
    evidence = Path(outcome.evidence_dir)
    validation = json.loads((evidence / "agents_validation.json").read_text(encoding="utf-8"))
    assert validation["agrees"] is False and validation["effect"].startswith("none")
    verdicts = json.loads((evidence / "policy_verdicts.json").read_text(encoding="utf-8"))["verdicts"]
    agent_candidate = [v for v in verdicts.values() if v["candidate"]["source"] == "agent:change_planner"]
    assert agent_candidate and agent_candidate[0]["eligible"] is False  # protected table, same gate as everyone
    claims = json.loads((evidence / "claims.json").read_text(encoding="utf-8"))
    diag = {c["claim"][:20]: c["verdict"] for c in claims["by_role"]["performance_diagnostician"]}
    assert diag["orders_by_customer d"] == "verified"
    assert diag["bench.ghosts is slow"] == "contradicted"


def test_the_agents_evidence_pack_uses_only_its_own_ids(cfg):
    prompts: list[str] = []

    def llm(prompt: str) -> str:
        prompts.append(prompt)
        return '{"findings": []}'

    outcome = run(cfg, FakeAdapter(make_target()), llm=llm)
    assert outcome.status == "RETAINED"
    pack = prompts[0].split("EVIDENCE:", 1)[1]
    assert "plan:" not in pack
    assert '"evidence_refs": ["E' in pack


def test_evidence_integrity_and_recomputation_are_reported_separately(cfg):
    adapter = FakeAdapter(make_target())
    outcome = run(cfg, adapter)
    report = verification(cfg, outcome.operation_id)
    assert report["integrity"]["all_intact"] is True
    assert report["attestation"]["status"] == "none"
    recomputed = recompute_decision(cfg, outcome.operation_id)
    assert recomputed["available"] and recomputed["matches"], recomputed
    # Tampering is detected, and never repaired silently.
    decision_path = next(Path(a["path"]) for a in Journal(cfg).artifacts(outcome.operation_id) if a["name"] == "baseline.json")
    decision_path.write_text(decision_path.read_text(encoding="utf-8").replace('"execution_ms": 6', '"execution_ms": 9'), encoding="utf-8")
    after = verification(cfg, outcome.operation_id)
    assert {f["name"]: f["integrity"] for f in after["integrity"]["files"]}["baseline.json"] == "modified"
