"""
A deterministic, in-memory stand-in for a PostgreSQL target, its adapter and its executor.

FakeServer holds what the real target holds: indexes, the change ledger, the
lease (advisory lock + fencing token) and Field Horizon sessions. FakeAdapter
observes and measures it; FakeExecutor changes it with the same typed
commands, refusal codes, fencing and ownership proof as executor.PostgresExecutor
(the proof function itself is shared). The same paths run against a real
PostgreSQL in test_dbeng_postgres.py.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fieldhorizon.config import AppConfig
from fieldhorizon.dbeng import executor as ex
from fieldhorizon.dbeng.model import ExplainResult, IndexState
from fieldhorizon.dbeng.targets import TargetConfig
from fieldhorizon.dbeng.workload import Workload, WorkloadQuery

LOOKUP = "SELECT id FROM bench.orders WHERE customer_id = %(c)s"
INSERT = "INSERT INTO bench.orders SELECT ..."
SUMMARY = "SELECT status, count(*) FROM bench.orders GROUP BY status"
CONTROL = "SELECT count(*) FROM bench.customers WHERE id BETWEEN %(lo)s AND %(lo)s + 50000"


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
                data_directory=".", measure_user="meas", owner_role="own", data_classification="synthetic")
    return TargetConfig(**{**base, **overrides})


def make_workload(write_weight: float = 1.0, control: bool = True) -> Workload:
    queries = [
        WorkloadQuery("lookup", "read", 50.0, LOOKUP, tuple({"c": i} for i in range(8))),
        WorkloadQuery("summary", "read", 1.0, SUMMARY, ({},), repeat=2),
        WorkloadQuery("insert", "write", write_weight, INSERT, ({},), repeat=8),
    ]
    if control:
        queries.append(WorkloadQuery("control", "read", 0.0, CONTROL, ({"lo": 1}, {"lo": 1000}), repeat=2, control=True))
    return Workload(name="fake", version=2, sha256="0" * 64, source=str(Path(__file__).parent / "fake_workload.yaml"),
                    queries=tuple(queries))


@dataclass
class FakeServer:
    """The target's state: what survives any client crash."""

    indexes: dict[str, IndexState] = field(default_factory=dict)
    ledger: dict[tuple[str, str], dict] = field(default_factory=dict)
    lease_token: int = 0
    lease_holder: dict | None = None        # lease row holder_* fields
    lock_owner: object | None = None        # the FakeExecutor whose "session" holds the advisory lock
    activity: list[dict] = field(default_factory=list)
    next_oid: int = 20000
    log: list[str] = field(default_factory=list)
    dead_tuples: int = 0
    # A long transaction still holding a lock on the table: every DROP INDEX CONCURRENTLY times out.
    drops_blocked: bool = False

    def ledger_rows(self, unfinished_only: bool = False) -> list[dict]:
        rows = [dict(r) for r in self.ledger.values()]
        return [r for r in rows if r["state"] not in ("retained", "reverted")] if unfinished_only else rows


class FakeAdapter:
    """bench.orders with a PK, an unindexed selective customer_id, and a write path that pays per index."""

    engine = "postgresql"

    def __init__(self, target, server: FakeServer | None = None, *, write_penalty=0.1, fail_on_explain_after_create=False,
                 identity=None, control_drift: float = 0.0):
        self.target = target
        self.server = server or FakeServer()
        self.write_penalty = write_penalty
        self.fail_after_create = fail_on_explain_after_create
        self.control_drift = control_drift
        self._tick = 0
        self.operation_id = None
        self._identity = identity or {"in_recovery": False, "instance_marker": "m-1", "system_identifier": "42", "version_num": 180006,
                                      "version": "PostgreSQL 18.6 (fake)"}

    @property
    def indexes(self) -> dict[str, IndexState]:
        return self.server.indexes

    @property
    def calls(self) -> list[str]:
        return self.server.log

    def bind_operation(self, operation_id):
        self.operation_id = operation_id

    def _jitter(self) -> float:
        self._tick += 1
        return 1.0 + 0.002 * (self._tick % 5)

    def _has_customer_index(self) -> IndexState | None:
        return next((s for s in self.server.indexes.values() if "customer_id" in s.columns and s.valid), None)

    def identity(self):
        return dict(self._identity)

    def discover(self):
        return {"captured_at": datetime.now(UTC).isoformat(), "availability": {"hypopg": {"status": "unavailable"}},
                "tables": [{"schema": "bench", "table": "orders", "kind": "table", "estimated_rows": 2_000_000, "total_bytes": 1,
                            "column_statistics": "available"}],
                "columns": [{"schema": "bench", "table": "orders", "column": c} for c in ("id", "customer_id", "status")],
                "indexes": [{"schema": "bench", "table": "orders", "index": "orders_pkey", "columns": ["id"], "is_valid": True,
                             "idx_scan": 0, "method": "btree", "is_unique": True, "is_primary": True, "is_ready": True,
                             "has_expressions": False, "is_partial": False, "definition": "CREATE UNIQUE INDEX orders_pkey ..."}],
                "constraints": [{"schema": "bench", "table": "orders", "name": "orders_pkey", "type": "p", "columns": ["id"]}],
                "table_statistics": []}

    def explain(self, sql, params=None, *, write=False):
        if self.fail_after_create and self.server.indexes:
            raise RuntimeError("connection lost")
        if write:
            self.server.dead_tuples += 1000
        if sql == LOOKUP:
            idx = self._has_customer_index()
            if idx:
                return ExplainResult(0.03 * self._jitter(), 0.1, [idx.name], ["Index Scan"], [], {"Plan": {"Filter": "x"}})
            scan = {"schema": "bench", "relation": "orders", "filter": f"(orders.customer_id = {params['c']})",
                    "predicates": [{"column": "customer_id", "op": "="}], "rows_returned": 10.0,
                    "rows_removed_by_filter": 1_999_990.0, "selectivity": 10 / 2_000_000}
            return ExplainResult(60.0 * self._jitter(), 0.1, [], ["Seq Scan"], [scan],
                                 {"Plan": {"Node Type": "Seq Scan", "Filter": f"(orders.customer_id = {params['c']})"}})
        if sql == INSERT:
            cost = 5.0 * (1 + self.write_penalty * len(self.server.indexes)) * self._jitter()
            return ExplainResult(cost, 0.1, [], ["Insert"], [], {"Plan": {}})
        if sql == CONTROL:
            drift = 1.0 + (self.control_drift if self.server.indexes else 0.0)
            return ExplainResult(4.0 * drift * self._jitter(), 0.1, ["customers_pkey"], ["Index Scan"], [], {"Plan": {}})
        scan = {"schema": "bench", "relation": "orders", "filter": None, "predicates": [], "rows_returned": 2e6,
                "rows_removed_by_filter": 0.0, "selectivity": 1.0}
        return ExplainResult(250.0 * self._jitter(), 0.1, [], ["Seq Scan"], [scan], {"Plan": {}})

    def execute_timed(self, sql, params=None, *, write=False):
        return self.explain(sql, params, write=write).execution_ms + 0.25

    def table_facts(self, schema, table):
        if (schema, table) == ("bench", "audit_log"):
            return {"exists": True, "kind": "table", "partitioning": "available", "estimated_rows": 100_000, "owner": "prot", "oid": 2,
                    "columns": ["id", "event_type"], "index_count": 1, "indexes": [], "dependency_status": "available",
                    "unverified_dependencies": [], "operator_can_create_index": False}
        if (schema, table) != ("bench", "orders"):
            return {"exists": False}
        return {"exists": True, "kind": "table", "partitioning": "available", "estimated_rows": 2_000_000, "owner": "own", "oid": 1,
                "columns": ["id", "customer_id", "status"], "index_count": 1 + len(self.server.indexes),
                "indexes": [{"index": "orders_pkey", "is_valid": True, "has_expressions": False, "is_partial": False,
                             "method": "btree", "columns": ["id"]}]
                + [{"index": s.name, "is_valid": s.valid, "has_expressions": False, "is_partial": False, "method": "btree",
                    "columns": list(s.columns)} for s in self.server.indexes.values()],
                "dependency_status": "available", "unverified_dependencies": [], "operator_can_create_index": True}

    def column_statistics(self, schema, table, columns):
        known = {"customer_id": {"n_distinct": 190_000.0, "mcf": [], "avg_width": 4},
                 "status": {"n_distinct": 5.0, "mcf": [0.2], "avg_width": 7},
                 "event_type": {"n_distinct": 6.0, "mcf": [0.17], "avg_width": 6}}
        return {c: known[c] for c in columns if c in known}

    def running_transactions(self):
        return []

    def index_state(self, schema, name):
        return self.server.indexes.get(name)

    def fieldhorizon_indexes(self):
        return list(self.server.indexes.values())

    def invalid_indexes(self):
        return [{"schema": s.schema, "index": s.name, "table": s.table} for s in self.server.indexes.values() if not s.valid]

    def lease_state(self, scope):
        holder = self.server.lease_holder or {}
        owner = self.server.lock_owner
        return {"status": "available", "detail": "",
                "row": {"scope": scope, "fencing_token": self.server.lease_token, **holder, "heartbeat_age_seconds": 0.0},
                "lock_held": owner is not None,
                "lock_holder": {"pid": 4242, "state": "idle", "application_name": "fh-dbeng/operator/x"} if owner is not None else None}

    def ledger_rows(self, *, unfinished_only=False):
        return self.server.ledger_rows(unfinished_only)

    def fieldhorizon_activity(self):
        return [dict(a) for a in self.server.activity]

    def index_build_progress(self):
        return []

    def state_snapshot(self, schemas=("bench",)):
        return {"captured_at": datetime.now(UTC).isoformat(), "database_bytes": 1000 + 10 * len(self.server.indexes),
                "tables": [{"schema": "bench", "table": "orders", "n_dead_tup": self.server.dead_tuples, "n_tup_ins": self.server.dead_tuples,
                            "n_tup_upd": 0, "n_tup_del": 0, "total_bytes": 100, "index_bytes": 10, "autovacuum_count": 0,
                            "autoanalyze_count": 0}],
                "sequences": [{"schema": "bench", "sequence": "orders_id_seq", "last_value": 2_000_000 + self.server.dead_tuples}]}

    def maintenance_activity(self):
        return []

    def statement_statistics(self, limit=25):
        return {"status": "available", "detail": "", "statements": []}

    def authority_facts(self, schemas=("bench",)):
        return {"roles": [], "memberships": [], "event_triggers": [], "dml_guards": [], "tables": [], "privileges": []}

    def close(self):
        pass


class FakeExecutor:
    """The executor contract over FakeServer: typed commands, fencing, freshness, policy/target checks, ownership proof."""

    def __init__(self, target, policy, *, operation_id, server: FakeServer, clock=time.monotonic, host_pid=None,
                 fail_create: str | None = None, fail_create_leaves_index: bool = True, cleanup_fails: bool = False):
        self.target = target
        self.policy = policy
        self.operation_id = operation_id
        self.server = server
        self.clock = clock
        self.lease: ex.Lease | None = None
        self.host_pid = host_pid or {"host": "fakehost", "pid": 1}
        self.fail_create = fail_create
        self.fail_create_leaves_index = fail_create_leaves_index
        self.cleanup_fails = cleanup_fails
        self.closed = False

    # --- lease ---
    def acquire(self):
        if self.server.lock_owner is not None and self.server.lock_owner is not self:
            raise ex.ExecutorRefusal(ex.LEASE_BUSY, f"held by {self.server.lease_holder}")
        self.server.lock_owner = self
        self.server.lease_token += 1
        self.server.lease_holder = {"holder_operation_id": self.operation_id, "holder_host": self.host_pid["host"],
                                    "holder_pid": self.host_pid["pid"], "released_at": None}
        self.lease = ex.Lease("index_optimization", self.operation_id, self.server.lease_token, 4242, datetime.now(UTC).isoformat())
        self.server.log.append(f"acquire:{self.operation_id}")
        return self.lease

    def check_lease(self):
        if self.closed or self.lease is None or self.server.lock_owner is not self or self.server.lease_token != self.lease.fencing_token:
            raise ex.ExecutorRefusal(ex.LEASE_LOST, "lease lost")

    def heartbeat(self):
        pass

    def release(self):
        if self.server.lock_owner is self:
            self.server.lock_owner = None
            if self.server.lease_holder:
                self.server.lease_holder["released_at"] = datetime.now(UTC).isoformat()
        self.lease = None
        self.closed = True

    def crash(self):
        """The process dies: the server ends the session, which releases the advisory lock."""
        if self.server.lock_owner is self:
            self.server.lock_owner = None
        self.closed = True

    def close(self):
        self.closed = True

    # --- authorization ---
    def authorize(self, *, eligibility=None):
        if self.lease is None:
            raise ex.ExecutorRefusal(ex.LEASE_LOST, "no lease")
        return ex.AuthorizationContext(
            operation_id=self.operation_id, target_name=self.target.name, target_fingerprint=self.target.fingerprint(),
            policy_sha256=self.policy.sha256, instance_marker=self.target.instance_marker, system_identifier=self.target.system_identifier,
            lease=self.lease, verified_monotonic=self.clock(), verified_at=datetime.now(UTC).isoformat(), eligibility=eligibility or {},
        )

    def verify_authorization(self, auth, operation, *, compensation=False):
        if operation not in self.policy.allowed_operations:
            raise ex.ExecutorRefusal(ex.OPERATION_NOT_ALLOWED, operation)
        if auth.lease != self.lease or auth.operation_id != self.operation_id:
            raise ex.ExecutorRefusal(ex.AUTHORIZATION_STALE, "other lease")
        if self.clock() - auth.verified_monotonic > self.policy.max_authorization_age_seconds:
            raise ex.ExecutorRefusal(ex.AUTHORIZATION_STALE, "too old")
        if not compensation and ex.sha256_file(self.policy.source) != auth.policy_sha256:
            raise ex.ExecutorRefusal(ex.POLICY_CHANGED, "policy file changed")
        self.check_lease()

    def ledger_row(self, operation_id, change_id):
        self.check_session()
        row = self.server.ledger.get((operation_id, change_id))
        return dict(row) if row else None

    def check_session(self):
        if self.closed:
            raise ex.ExecutorRefusal(ex.LEASE_LOST, "session gone")

    # --- commands ---
    def create_index(self, auth, cmd):
        ex.templates.create_index_statement(cmd.schema, cmd.table, cmd.columns, cmd.index_name)
        self.verify_authorization(auth, ex.OP_CREATE_INDEX)
        if cmd.index_name in self.server.indexes:
            raise ex.ExecutorRefusal(ex.OBJECT_STATE_CHANGED, "exists")
        self.server.ledger[(self.operation_id, cmd.change_id)] = {
            "operation_id": self.operation_id, "change_id": cmd.change_id, "fencing_token": auth.lease.fencing_token,
            "index_schema": cmd.schema, "index_name": cmd.index_name, "table_name": cmd.table, "columns": list(cmd.columns),
            "state": "applying", "index_oid": None, "note": None, "recovered_by": None, "holder_host": self.host_pid["host"],
        }
        self.server.log.append(f"create:{cmd.index_name}")
        if self.fail_create:
            if self.fail_create_leaves_index:
                self._put(cmd, valid=False, marker=None)
            if self.cleanup_fails:
                raise ex.ExecutionFailed(self.fail_create, "simulated", cleaned=False)
            self.server.indexes.pop(cmd.index_name, None)
            self.server.ledger[(self.operation_id, cmd.change_id)]["state"] = "reverted"
            raise ex.ExecutionFailed(self.fail_create, "simulated", cleaned=True)
        state = self._put(cmd, valid=True, marker={"fieldhorizon": "dbeng", "operation_id": self.operation_id, "change_id": cmd.change_id})
        row = self.server.ledger[(self.operation_id, cmd.change_id)]
        row.update(state="applied", index_oid=state.oid)
        return state

    def _put(self, cmd, *, valid, marker):
        self.server.next_oid += 1
        state = IndexState(cmd.schema, cmd.index_name, cmd.table, valid, valid,
                           f"CREATE INDEX {cmd.index_name} ON {cmd.schema}.{cmd.table} USING btree ({', '.join(cmd.columns)})",
                           marker, 18_000_000, oid=self.server.next_oid, method="btree", columns=tuple(cmd.columns))
        self.server.indexes[cmd.index_name] = state
        return state

    def drop_own_index(self, auth, cmd, *, compensation=True):
        self.verify_authorization(auth, ex.OP_DROP_OWN_INDEX, compensation=compensation)
        row = self.server.ledger.get((cmd.owner_operation_id, cmd.change_id))
        if row is None:
            raise ex.ExecutorRefusal(ex.OWNERSHIP_UNPROVEN, "no ledger row")
        if row["state"] == "reverted":
            return {"dropped": False, "already_reverted": True}
        if row["state"] == "retained":
            raise ex.ExecutorRefusal(ex.NOT_ELIGIBLE, "retained")
        state = self.server.indexes.get(cmd.index_name)
        if state is None:
            row.update(state="reverted", recovered_by=cmd.recovery_ref)
            return {"dropped": False, "absent": True}
        proof = ex.ownership_proof(state, row)
        if not proof.proven:
            raise ex.ExecutorRefusal(ex.OWNERSHIP_UNPROVEN, "; ".join(proof.reasons))
        if self.server.drops_blocked:
            row.update(state="reverting")
            raise ex.ExecutionFailed(ex.DDL_LOCK_TIMEOUT, "canceling statement due to lock timeout", cleaned=False)
        del self.server.indexes[cmd.index_name]
        self.server.log.append(f"drop:{cmd.index_name}")
        row.update(state="reverted", recovered_by=cmd.recovery_ref, note=cmd.reason)
        return {"dropped": True, "was_valid": state.valid, "had_marker": state.marker is not None, "proof": proof.describe()}

    def mark_retained(self, auth, change_id):
        self.verify_authorization(auth, ex.OP_CREATE_INDEX)
        self.server.ledger[(self.operation_id, change_id)]["state"] = "retained"

    def analyze_table(self, auth, cmd):
        self.verify_authorization(auth, ex.OP_ANALYZE)
        self.server.log.append(f"analyze:{cmd.table}")

    def vacuum_table(self, auth, cmd):
        self.verify_authorization(auth, ex.OP_VACUUM)
        self.server.dead_tuples = 0


def executor_factory(target, policy, server: FakeServer, **kwargs):
    made: list[FakeExecutor] = []

    def make(operation_id):
        executor = FakeExecutor(target, policy, operation_id=operation_id, server=server, **kwargs)
        made.append(executor)
        return executor

    make.made = made  # type: ignore[attr-defined]
    return make
