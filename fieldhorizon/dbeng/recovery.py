"""
Unfinished changes: detection (read-only) and recovery (under the target lease).

Truth lives on the target. The change ledger and lease in fieldhorizon_meta
are shared by every Field Horizon process, whatever its local journal; the
local journal only adds this process's own account of its operations.

detect()  -- observer only, changes nothing. Lists what is unfinished: ledger
             rows not retained/reverted, local operations without a terminal
             status, Field Horizon sessions still active on the server after
             their client died, invalid or unledgered fhx_ indexes.
recover() -- acquires the lease (or uses the caller's), waits a bounded time
             for orphaned server activity to end (a CREATE INDEX CONCURRENTLY
             keeps running server-side after its client is killed on Windows),
             then brings each unfinished ledger row to 'reverted' through
             executor.drop_own_index -- which drops only what it can prove is
             this system's change. Anything unproven is reported and left alone.

Three ways in, recorded on every run: automatic_startup (service start),
automatic_preflight (a new operation found unfinished state), manual (an
operator asked). A detection without recovery is reported as such.
Idempotent: a second run finds nothing to do.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from ..config import AppConfig
from ..events import ACTOR_CLI, OperationEmitter
from . import status as st
from .executor import DropOwnIndex, ExecutionFailed, ExecutorRefusal
from .journal import Journal
from .procs import pid_alive
from .targets import TargetConfig, redact

MODE_AUTOMATIC_STARTUP = "automatic_startup"
MODE_AUTOMATIC_PREFLIGHT = "automatic_preflight"
MODE_MANUAL = "manual"
MODES = (MODE_AUTOMATIC_STARTUP, MODE_AUTOMATIC_PREFLIGHT, MODE_MANUAL)

KIND_RECOVERY = "recovery"
RECOVERY_DONE, RECOVERY_NOTHING, RECOVERY_BLOCKED, RECOVERY_DEFERRED = (
    st.RECOVERY_DONE, st.RECOVERY_NOTHING, st.RECOVERY_BLOCKED, st.RECOVERY_DEFERRED)
EVT_DB_RECOVERY_COMPLETED, EVT_DB_OPERATION_RECOVERED = st.EVT_RECOVERY_COMPLETED, st.EVT_RECOVERED
STATUS_RECOVERED_REVERTED, STATUS_FAILED_RECOVERED, STATUS_FAILED_NEEDS_RECOVERY = (
    st.RECOVERED_REVERTED, st.FAILED_RECOVERED, st.FAILED_NEEDS_RECOVERY)


@dataclass
class Incident:
    kind: str
    severity: str          # info | warning | blocking
    subject: str
    detail: str
    recoverable: bool
    data: dict = field(default_factory=dict)

    def describe(self) -> dict:
        return asdict(self)


def new_recovery_id() -> str:
    import uuid

    return f"RC-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _holder(lease: dict) -> dict | None:
    """The live lease holder, with whether its client process is known alive (True), dead (False) or unknown (None)."""
    row = lease.get("row") or {}
    if not lease.get("lock_held"):
        return None
    return {
        "operation_id": row.get("holder_operation_id"),
        "host": row.get("holder_host"),
        "pid": row.get("holder_pid"),
        "backend_pid": (lease.get("lock_holder") or {}).get("pid"),
        "journal": row.get("holder_journal"),
        "heartbeat_age_seconds": row.get("heartbeat_age_seconds"),
        "client_alive": pid_alive(row.get("holder_pid"), row.get("holder_host")),
        "backend_state": (lease.get("lock_holder") or {}).get("state"),
        "backend_query": (lease.get("lock_holder") or {}).get("query"),
    }


def detect(adapter, journal: Journal, target: TargetConfig, policy, *, exclude: tuple[str, ...] = ()) -> dict:
    """Read-only. Everything unfinished on the target and in this journal, with what recovery could do about it."""
    lease = adapter.lease_state(policy.lease_scope)
    holder = _holder(lease)
    live_op = holder["operation_id"] if holder and holder["client_alive"] is not False else None
    incidents: list[Incident] = []

    if lease.get("status") != "available" or lease.get("row") is None:
        incidents.append(Incident("coordination_unavailable", "blocking", target.name,
                                  f"no lease row readable for scope {policy.lease_scope!r}: {lease.get('detail')}", False))
    if holder and holder["client_alive"] is False:
        incidents.append(Incident(
            "orphan_activity", "blocking", f"backend {holder['backend_pid']}",
            f"the lease is held by backend {holder['backend_pid']} for {holder['operation_id']}, whose client process "
            f"(pid {holder['pid']} on {holder['host']}) is gone; the server is probably finishing its last statement",
            True, {"holder": holder},
        ))

    activity = [a for a in adapter.fieldhorizon_activity()
                if a.get("operation_id") not in (None, "idle", live_op, *exclude)]
    for a in activity:
        if a.get("state") == "idle":
            continue
        incidents.append(Incident(
            "orphan_activity", "blocking", f"backend {a['pid']}",
            f"{a['application_name']} still {a['state']} for {round(a.get('query_seconds') or 0)}s: {a.get('query', '')[:120]}",
            True, {"activity": a},
        ))

    ledger = adapter.ledger_rows(unfinished_only=True)
    for row in ledger:
        if row["operation_id"] == live_op or row["operation_id"] in exclude:
            continue
        incidents.append(Incident(
            "ledger_unfinished", "blocking", f"{row['index_schema']}.{row['index_name']}",
            f"change {row['operation_id']}/{row['change_id']} left in state {row['state']!r} by {row.get('holder_host')}",
            True, {"ledger": row},
        ))

    ledgered = {(r["index_schema"], r["index_name"]) for r in adapter.ledger_rows()}
    for state in adapter.fieldhorizon_indexes():
        if (state.schema, state.name) not in ledgered:
            incidents.append(Incident(
                "unledgered_index", "warning", f"{state.schema}.{state.name}",
                "named like a Field Horizon index but absent from every ledger: not provably ours, never dropped automatically",
                False, {"index": state.describe()},
            ))
    for inv in adapter.invalid_indexes():
        if (inv["schema"], inv["index"]) not in ledgered:
            incidents.append(Incident("invalid_index", "warning", f"{inv['schema']}.{inv['index']}",
                                      "invalid index not created by Field Horizon: reported, not touched", False, {"index": inv}))

    for record in journal.unfinished():
        if record.target != target.name or record.operation_id in exclude or record.operation_id == live_op:
            continue
        runtime = journal.runtime(record.operation_id) or {}
        alive = pid_alive(runtime.get("holder_pid"), runtime.get("holder_host"))
        if alive and record.operation_id != live_op and runtime.get("lease_token") is None:
            # Alive and not yet holding the lease: still starting. Not an incident.
            continue
        incidents.append(Incident(
            "journal_unfinished", "blocking", record.operation_id,
            f"{record.kind} left in state {record.state!r} (status {record.status}) by pid {runtime.get('holder_pid')}",
            True, {"operation": {"operation_id": record.operation_id, "kind": record.kind, "state": record.state,
                                 "status": record.status, "started_at": record.started_at}, "client_alive": alive},
        ))
    for record in journal.recent(limit=500):
        if record.target == target.name and record.status == STATUS_FAILED_NEEDS_RECOVERY and record.operation_id not in exclude:
            incidents.append(Incident("needs_recovery", "blocking", record.operation_id,
                                      f"operation ended FAILED_NEEDS_RECOVERY: {record.message}", True,
                                      {"operation": {"operation_id": record.operation_id, "message": record.message}}))

    blocking = [i for i in incidents if i.severity == "blocking"]
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "target": target.name,
        "lease": {k: lease.get(k) for k in ("status", "detail", "row", "lock_held")},
        "holder": holder,
        "operation_in_progress": live_op,
        "incidents": [i.describe() for i in incidents],
        "blocking": len(blocking),
        "recoverable": all(i.recoverable for i in blocking),
        "clean": not blocking,
    }


def _wait_for_orphans(adapter, target: TargetConfig, policy, exclude: tuple[str, ...], journal: Journal,
                      sleep: Callable[[float], None]) -> dict:
    deadline = time.monotonic() + float(policy.orphan_wait_seconds)
    waited = 0.0
    while True:
        detection = detect(adapter, journal, target, policy, exclude=exclude)
        orphans = [i for i in detection["incidents"] if i["kind"] == "orphan_activity"]
        if not orphans or time.monotonic() >= deadline:
            return {"waited_seconds": round(waited, 1), "remaining": orphans, "detection": detection}
        sleep(1.0)
        waited += 1.0


def recover(
    cfg: AppConfig,
    target: TargetConfig,
    adapter,
    policy,
    executor_factory,
    *,
    mode: str = MODE_MANUAL,
    actor: str = ACTOR_CLI,
    acting_executor=None,
    acting_operation: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    if mode not in MODES:
        raise ValueError(f"unknown recovery mode {mode!r}")
    journal = Journal(cfg)
    run_id = new_recovery_id()
    journal.create(run_id, KIND_RECOVERY, target.name, {"mode": mode, "acting_operation": acting_operation},
                   policy.sha256, None)
    from .procs import current_process

    proc = current_process()
    journal.set_runtime(run_id, holder_host=proc["host"], holder_pid=proc["pid"], process_started_at=proc["started_at"],
                        recovery_mode=mode)
    emitter = OperationEmitter(cfg, actor=actor, aggregate_type="db_operation", correlation_id=run_id)
    exclude = tuple(x for x in (run_id, acting_operation) if x)
    actions: list[dict] = []
    blocked: list[dict] = []

    waited = _wait_for_orphans(adapter, target, policy, exclude, journal, sleep)
    detection = waited["detection"]
    if waited["remaining"]:
        blocked.extend({"incident": i, "action": "waited for orphaned server activity; still running"} for i in waited["remaining"])
        return _finish(journal, emitter, run_id, mode, RECOVERY_BLOCKED,
                       f"orphaned server activity still running after {waited['waited_seconds']}s; nothing was changed",
                       detection, actions, blocked, waited)

    to_fix = [i for i in detection["incidents"] if i["kind"] in ("ledger_unfinished", "journal_unfinished", "needs_recovery")]
    if not to_fix:
        status = RECOVERY_DEFERRED if detection["operation_in_progress"] else RECOVERY_NOTHING
        message = (f"operation {detection['operation_in_progress']} is running; its changes are its own"
                   if detection["operation_in_progress"] else "nothing unfinished")
        return _finish(journal, emitter, run_id, mode, status, message, detection, actions, blocked, waited)

    executor = acting_executor
    own_lease = executor is None
    try:
        if own_lease:
            executor = executor_factory(run_id)
            try:
                executor.acquire()
            except ExecutorRefusal as exc:
                blocked.append({"action": "acquire lease", "code": exc.code, "detail": exc.detail})
                return _finish(journal, emitter, run_id, mode, RECOVERY_BLOCKED, f"lease unavailable: {exc.detail}",
                               detection, actions, blocked, waited)
        ledger_by_op: dict[str, list[dict]] = {}
        for row in adapter.ledger_rows(unfinished_only=True):
            if row["operation_id"] in exclude:
                continue
            ledger_by_op.setdefault(row["operation_id"], []).append(row)
            try:
                auth = executor.authorize()
                result = executor.drop_own_index(auth, DropOwnIndex(
                    owner_operation_id=row["operation_id"], change_id=row["change_id"], schema=row["index_schema"],
                    index_name=row["index_name"], reason=f"recovery ({mode}): change left in state {row['state']!r}",
                    recovery_ref=run_id,
                ), compensation=True)
                actions.append({"change": f"{row['operation_id']}/{row['change_id']}", "index": f"{row['index_schema']}.{row['index_name']}",
                                "previous_state": row["state"], "result": "dropped" if result.get("dropped") else "already absent",
                                "proof": result.get("proof"), "was_valid": result.get("was_valid"), "had_marker": result.get("had_marker")})
            except ExecutorRefusal as exc:
                blocked.append({"change": f"{row['operation_id']}/{row['change_id']}", "index": f"{row['index_schema']}.{row['index_name']}",
                                "code": exc.code, "detail": exc.detail})
            except ExecutionFailed as exc:
                blocked.append({"change": f"{row['operation_id']}/{row['change_id']}", "index": f"{row['index_schema']}.{row['index_name']}",
                                "code": exc.code, "detail": exc.detail, "cleanup": exc.cleanup})

        blocked_ops = {b["change"].split("/")[0] for b in blocked if "change" in b}
        for record in journal.unfinished() + [r for r in journal.recent(limit=500) if r.status == STATUS_FAILED_NEEDS_RECOVERY]:
            if record.target != target.name or record.operation_id in exclude or record.operation_id in blocked_ops:
                continue
            if record.operation_id == detection["operation_in_progress"]:
                continue
            undone = [a for a in actions if a["change"].startswith(record.operation_id + "/")]
            if record.kind == KIND_RECOVERY:
                journal.finish(record.operation_id, RECOVERY_BLOCKED, f"recovery run interrupted; superseded by {run_id}")
                continue
            new_status = STATUS_RECOVERED_REVERTED if record.finished_at is None else STATUS_FAILED_RECOVERED
            message = (f"Recovered ({mode}, run {run_id}) after interruption in state {record.state!r}: "
                       + (f"reverted {[a['index'] for a in undone]}" if undone else "no change of this operation remained on the target"))
            journal.finish(record.operation_id, new_status, message)
            journal.set_runtime(record.operation_id, recovery_mode=mode, recovered_by=run_id)
            emitter.emit(EVT_DB_OPERATION_RECOVERED, aggregate_id=record.operation_id,
                         payload={"status": new_status, "mode": mode, "recovery_run": run_id, "undone": [a["index"] for a in undone]})
            actions.append({"operation": record.operation_id, "result": new_status})
        status = RECOVERY_BLOCKED if blocked else RECOVERY_DONE
        message = (f"{len([a for a in actions if 'change' in a])} change(s) brought to 'reverted'"
                   + (f"; {len(blocked)} left alone (unproven or failed)" if blocked else ""))
        return _finish(journal, emitter, run_id, mode, status, message, detection, actions, blocked, waited)
    finally:
        if own_lease and executor is not None:
            executor.release()


def _finish(journal: Journal, emitter, run_id: str, mode: str, status: str, message: str, detection: dict,
            actions: list[dict], blocked: list[dict], waited: dict) -> dict:
    report = {"run_id": run_id, "mode": mode, "status": status, "message": message, "actions": actions, "blocked": blocked,
              "orphan_wait": {k: waited[k] for k in ("waited_seconds", "remaining")}, "detection": detection}
    journal.write_artifact(run_id, "recovery.json", "recovery", report)
    journal.finish(run_id, status, message, {"actions": actions, "blocked": blocked})
    journal.set_runtime(run_id, cause_code=None if status in (RECOVERY_DONE, RECOVERY_NOTHING) else status.lower())
    emitter.emit(EVT_DB_RECOVERY_COMPLETED, aggregate_id=run_id, payload={"status": status, "mode": mode, "message": message})
    return report


def safe_detect(adapter, journal: Journal, target: TargetConfig, policy) -> dict:
    """detect() for display: an unreachable target is reported, not raised."""
    try:
        return detect(adapter, journal, target, policy)
    except Exception as exc:  # noqa: BLE001 - reported to the operator as-is (redacted)
        return {"checked_at": datetime.now(UTC).isoformat(), "target": target.name, "error": redact(f"{type(exc).__name__}: {exc}"),
                "incidents": [], "blocking": None, "recoverable": None, "clean": None, "holder": None, "operation_in_progress": None}
