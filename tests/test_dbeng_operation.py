"""
The autonomous operation state machine against a deterministic in-memory
adapter: every terminal path, the journal, events, evidence hashes, and
crash recovery. The same paths run against a real PostgreSQL in
test_dbeng_postgres.py.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.dbeng.journal import Journal
from fieldhorizon.dbeng.model import ExplainResult, IndexState
from fieldhorizon.dbeng.operation import IndexOptimization, OptimizeRequest, recover
from fieldhorizon.dbeng.policy import load_policy
from fieldhorizon.dbeng.targets import TargetConfig
from fieldhorizon.dbeng.workload import Workload, WorkloadQuery
from fieldhorizon.events import EventRepository

POLICY = load_policy()
LOOKUP = "SELECT id FROM bench.orders WHERE customer_id = %(c)s"
INSERT = "INSERT INTO bench.orders SELECT ..."
SUMMARY = "SELECT status, count(*) FROM bench.orders GROUP BY status"


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        root=tmp_path, database=tmp_path / "fh.sqlite3", books=tmp_path / "b", json_corpus=tmp_path / "j",
        outputs=tmp_path / "outputs", logs=tmp_path / "logs", ollama_base_url="http://localhost:11434",
        default_model="hermes3:8b", temperature=1.0, top_p=0.9, repeat_penalty=1.0, num_ctx=2048,
        book_fragments=1, json_entries=1, chunk_chars=100, chunk_overlap=10, tone="t", mode="m",
        manifestos=tmp_path / "m", embedding_model="nomic-embed-text",
    )


def make_target(**overrides) -> TargetConfig:
    base = dict(name="fake", engine="postgresql", host="127.0.0.1", port=1, dbname="fake", observer_user="obs",
                operator_user="op", environment="isolated_test", instance_marker="m-1", system_identifier="42",
                # A real directory, so the free-space check is verifiable (unknown storage blocks by policy).
                data_directory=".")
    return TargetConfig(**{**base, **overrides})


def make_workload(write_weight: float = 1.0) -> Workload:
    return Workload(name="fake", version=1, sha256="0" * 64, source="memory", queries=(
        WorkloadQuery("lookup", "read", 50.0, LOOKUP, tuple({"c": i} for i in range(8))),
        WorkloadQuery("summary", "read", 1.0, SUMMARY, ({},), repeat=2),
        WorkloadQuery("insert", "write", write_weight, INSERT, ({},), repeat=8),
    ))


class FakeAdapter:
    """A table bench.orders with a PK, an unindexed selective customer_id, and a write path that pays per index."""

    engine = "postgresql"

    def __init__(self, target, *, write_penalty=0.1, fail_on_explain_after_create=False, identity=None):
        self.target = target
        self.indexes: dict[str, IndexState] = {}
        self.write_penalty = write_penalty
        self.fail_after_create = fail_on_explain_after_create
        self.calls: list[str] = []
        self._tick = 0
        self._identity = identity or {"in_recovery": False, "instance_marker": "m-1", "system_identifier": "42", "version_num": 180006}

    def _jitter(self) -> float:
        self._tick += 1
        return 1.0 + 0.002 * (self._tick % 5)

    def _has_customer_index(self) -> IndexState | None:
        return next((s for s in self.indexes.values() if "customer_id" in s.definition and s.valid), None)

    def identity(self):
        return dict(self._identity)

    def discover(self):
        return {"captured_at": "2026-11-03T00:00:00+00:00", "availability": {"hypopg": {"status": "unavailable"}},
                "tables": [], "indexes": [], "constraints": [], "table_statistics": []}

    def explain(self, sql, params=None, *, write=False):
        if self.fail_after_create and self.indexes:
            raise RuntimeError("connection lost")
        if sql == LOOKUP:
            idx = self._has_customer_index()
            if idx:
                return ExplainResult(0.03 * self._jitter(), 0.1, [idx.name], ["Index Scan"], [], {"Plan": {}})
            scan = {"schema": "bench", "relation": "orders", "filter": f"(orders.customer_id = {params['c']})",
                    "predicates": [{"column": "customer_id", "op": "="}], "rows_returned": 10.0,
                    "rows_removed_by_filter": 1_999_990.0, "selectivity": 10 / 2_000_000}
            return ExplainResult(60.0 * self._jitter(), 0.1, [], ["Seq Scan"], [scan], {"Plan": {}})
        if sql == INSERT:
            cost = 5.0 * (1 + self.write_penalty * len(self.indexes)) * self._jitter()
            return ExplainResult(cost, 0.1, [], ["Insert"], [], {"Plan": {}})
        scan = {"schema": "bench", "relation": "orders", "filter": None, "predicates": [], "rows_returned": 2e6,
                "rows_removed_by_filter": 0.0, "selectivity": 1.0}
        return ExplainResult(250.0 * self._jitter(), 0.1, [], ["Seq Scan"], [scan], {"Plan": {}})

    def table_facts(self, schema, table):
        if (schema, table) == ("bench", "audit_log"):
            return {"exists": True, "kind": "table", "partitioning": "available", "estimated_rows": 100_000, "owner": "own",
                    "columns": ["id", "event_type"], "index_count": 1, "indexes": [], "dependency_status": "available",
                    "unverified_dependencies": [], "operator_can_create_index": True}
        if (schema, table) != ("bench", "orders"):
            return {"exists": False}
        return {"exists": True, "kind": "table", "partitioning": "available", "estimated_rows": 2_000_000, "owner": "own",
                "columns": ["id", "customer_id", "status"], "index_count": 1 + len(self.indexes),
                "indexes": [{"index": "orders_pkey", "is_valid": True, "has_expressions": False, "is_partial": False,
                             "method": "btree", "columns": ["id"]}],
                "dependency_status": "available", "unverified_dependencies": [], "operator_can_create_index": True}

    def column_statistics(self, schema, table, columns):
        known = {"customer_id": {"n_distinct": 190_000.0, "mcf": [], "avg_width": 4},
                 "status": {"n_distinct": 5.0, "mcf": [0.2], "avg_width": 7},
                 "event_type": {"n_distinct": 6.0, "mcf": [0.17], "avg_width": 6}}
        return {c: known[c] for c in columns if c in known}

    def running_transactions(self):
        return []

    def index_state(self, schema, name):
        return self.indexes.get(name)

    def fieldhorizon_indexes(self):
        return list(self.indexes.values())

    def create_index_concurrently(self, spec, marker, *, lock_timeout_ms, statement_timeout_ms):
        self.calls.append(f"create:{spec.name}")
        state = IndexState(spec.schema, spec.name, spec.table, True, True,
                           f"CREATE INDEX {spec.name} ON bench.orders USING btree ({', '.join(spec.columns)})",
                           {"fieldhorizon": "dbeng", **marker}, 18_000_000)
        self.indexes[spec.name] = state
        return state

    def drop_index_concurrently(self, schema, name, *, lock_timeout_ms):
        self.calls.append(f"drop:{name}")
        self.indexes.pop(name, None)
        return True

    def analyze_table(self, schema, table):
        self.calls.append(f"analyze:{table}")

    def close(self):
        pass


class SimulatedCrash(BaseException):
    """Escapes the operation's `except Exception` exactly like a killed process: no cleanup runs."""


@pytest.fixture
def cfg(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    return cfg


def run(cfg, adapter, *, target=None, workload=None, candidates=None, llm=None, fault_after=None, fault_hook=None):
    target = target or adapter.target
    request = OptimizeRequest(target=target.name, workload_path="memory", candidates=candidates or [],
                              use_llm=llm is not None, fault_after=fault_after)
    return IndexOptimization(cfg, target, adapter, POLICY, workload or make_workload(), request,
                             llm=llm, fault_hook=fault_hook).run()


def test_a_justified_index_is_built_measured_and_retained(cfg):
    adapter = FakeAdapter(make_target())
    outcome = run(cfg, adapter)

    assert outcome.status == "RETAINED", outcome.message
    assert [c.split(":")[0] for c in adapter.calls] == ["create", "analyze"]
    assert len(adapter.indexes) == 1
    journal = Journal(cfg)
    steps = [(s["step"], s["status"]) for s in journal.steps(outcome.operation_id)]
    assert steps == [(s, "completed") for s in ("preflight", "discover", "baseline", "diagnose", "validate", "apply", "measure", "decide", "verify")]
    assert [c["state"] for c in journal.changes(outcome.operation_id)] == ["retained"]
    for artifact in journal.artifacts(outcome.operation_id):
        assert hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest() == artifact["sha256"]
    events = [e.event_type for e in EventRepository(cfg).by_correlation(outcome.operation_id)]
    assert events[0] == "DbOperationStarted" and events[-1] == "DbOperationCompleted"
    assert "DbChangeApplied" in events and "DbDecisionRecorded" in events
    with connect(cfg.database) as conn:
        assert conn.execute("SELECT count(*) FROM provenance_edges WHERE provenance_ref = ?", (outcome.operation_id,)).fetchone()[0] >= 2


def test_rerunning_after_a_retained_change_is_a_no_op(cfg):
    adapter = FakeAdapter(make_target())
    assert run(cfg, adapter).status == "RETAINED"
    again = run(cfg, adapter)
    assert again.status == "NO_CANDIDATE"
    assert [c for c in adapter.calls if c.startswith("create")] == adapter.calls[:1]


def test_a_measured_regression_is_reverted(cfg):
    adapter = FakeAdapter(make_target(), write_penalty=1.0)
    outcome = run(cfg, adapter, workload=make_workload(write_weight=1000.0))
    assert outcome.status == "REVERTED"
    assert adapter.indexes == {}
    assert [c["state"] for c in Journal(cfg).changes(outcome.operation_id)] == ["reverted"]
    failed = {c["name"] for c in outcome.decision["criteria"] if c["status"] != "pass"}
    assert "weighted_workload_improved" in failed


@pytest.mark.parametrize("target_change", [{"environment": "production"}, {"instance_marker": "someone-else"}])
def test_no_write_without_authority(cfg, target_change):
    adapter = FakeAdapter(make_target(**target_change))
    outcome = run(cfg, adapter)
    assert outcome.status == "BLOCKED"
    assert adapter.calls == []


def test_poor_and_prohibited_candidates_are_rejected_without_touching_the_database(cfg):
    adapter = FakeAdapter(make_target())
    no_scan_workload = Workload(name="w", version=1, sha256="1" * 64, source="m",
                                queries=(WorkloadQuery("summary", "read", 1.0, SUMMARY, ({},), repeat=2),))
    outcome = run(cfg, adapter, workload=no_scan_workload, candidates=["bench.orders(status)", "bench.audit_log(event_type)"])
    assert outcome.status == "REJECTED"
    assert adapter.calls == []
    verdicts = json.loads(Path(outcome.evidence_dir, "policy_verdicts.json").read_text(encoding="utf-8"))["verdicts"]
    blocking = sorted(b for v in verdicts.values() for b in v["blocking"])
    assert blocking == ["leading_column_selective", "leading_column_selective", "table_not_protected"]


def test_a_failure_after_the_change_undoes_it_and_says_so(cfg):
    adapter = FakeAdapter(make_target(), fail_on_explain_after_create=True)
    outcome = run(cfg, adapter)
    assert outcome.status == "FAILED_RECOVERED"
    assert adapter.indexes == {}
    assert "connection lost" in outcome.message
    assert [c["state"] for c in Journal(cfg).changes(outcome.operation_id)] == ["reverted"]


def test_a_crash_after_apply_is_recovered_by_reverting_and_recovery_is_idempotent(cfg):
    adapter = FakeAdapter(make_target())

    def crash(step):
        raise SimulatedCrash(step)

    with pytest.raises(SimulatedCrash):
        run(cfg, adapter, fault_after="apply", fault_hook=crash)
    journal = Journal(cfg)
    [interrupted] = journal.unfinished()
    assert interrupted.state == "apply"
    assert len(adapter.indexes) == 1

    blocked = run(cfg, adapter)
    assert blocked.status == "BLOCKED" and "recover" in blocked.message

    actions = recover(cfg, make_target(), adapter, POLICY)
    assert adapter.indexes == {}
    assert journal.get(interrupted.operation_id).status == "RECOVERED_REVERTED"
    assert any(a.get("status") == "RECOVERED_REVERTED" for a in actions)
    assert recover(cfg, make_target(), adapter, POLICY) == []

    assert run(cfg, adapter).status == "RETAINED"


def test_recovery_never_drops_an_unmarked_index_it_cannot_prove_is_ours(cfg):
    adapter = FakeAdapter(make_target())
    adapter.indexes["fhx_orders_somebody"] = IndexState("bench", "fhx_orders_somebody", "orders", True, True, "CREATE INDEX ...", None, 1)
    actions = recover(cfg, make_target(), adapter, POLICY)
    assert "fhx_orders_somebody" in adapter.indexes
    assert actions[0]["action"].startswith("left alone")


def test_llm_agents_inform_but_never_decide(cfg):
    replies = {
        "change planner": '{"proposals": [{"schema": "bench", "table": "audit_log", "columns": ["event_type"], "evidence": ["E1"]}]}',
        "adversarial reviewer": '{"objections": [{"candidate_id": "x", "type": "write_overhead", "objection": "o", "evidence": ["E1"]}]}',
        "independent validator": '{"assessment": "revert", "findings": [{"claim": "c", "evidence": ["E1"]}]}',
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
