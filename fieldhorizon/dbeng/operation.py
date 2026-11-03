"""
Autonomous index optimization: one authorized operation, end to end, with
no human approving intermediate steps -- and no step deciding its own success.

    preflight  -> authority, target lease (all processes), unfinished state
                  (recovered automatically when provable, otherwise BLOCKED)
    discover   -> versioned snapshot + deterministic structural findings
    baseline   -> workload measured before any change (A/A halves for noise)
    diagnose   -> index candidates from the measured plans (+ operator ones)
    analyze    -> LLM roles over the evidence (optional; advisory only)
    validate   -> policy.evaluate_candidate for every candidate
    authorize  -> re-verification right before the change: identity, policy
                  file, target file, fresh facts, eligibility, lease
    apply      -> executor: ledger intent, CREATE INDEX CONCURRENTLY, marker
    measure    -> the same workload again
    decide     -> policy.decide: RETAIN only if every criterion passes
    revert     -> executor: DROP INDEX CONCURRENTLY of the proven own change,
                  then a post-compensation check
    verify     -> catalog and target ledger match the decision
    report     -> evidence bundle + markdown report, all hashed in the journal

Operators can pause, resume or cancel: requests are journaled and take effect
at the next safe point (between steps and between measurement rounds, never
inside a DDL statement). A cancelled operation removes its change. A failure
is never a silent success: it ends FAILED_RECOVERED (target confirmed back to
its prior state) or FAILED_NEEDS_RECOVERY (with the reason recovery is needed).
"""
from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..events import ACTOR_CLI, OperationEmitter
from ..provenance import REL_DERIVED_FROM, REL_OPPOSES, REL_SUPPORTS, ProvenanceEdgeRepository
from . import agents as ag
from . import status as st
from .advisor import parse_candidate_argument, propose_index_candidates, structural_findings
from .claims import verify_agent_reports
from .docs import documentary_evidence
from .executor import (
    OP_CREATE_INDEX,
    AnalyzeTable,
    AuthorizationContext,
    CreateIndex,
    DropOwnIndex,
    ExecutionFailed,
    ExecutorRefusal,
    VacuumTable,
    sha256_file,
)
from .journal import Journal, evidence_dir
from .model import Candidate, IndexState
from .policy import RETAIN, Policy, authorize_write, decide, evaluate_candidate, free_disk_bytes
from .procs import current_process
from .targets import TargetConfig, redact
from .workload import Measurement, Workload, WorkloadQuery, run_workload

KIND_INDEX_OPTIMIZATION = "index_optimization"
AGGREGATE_DB_OPERATION = "db_operation"
OBJECT_DB_OPERATION = "db_operation"
OBJECT_DB_CANDIDATE = "db_candidate"
OBJECT_DB_EVIDENCE = "db_evidence"
CHANGE_ID = "CH-1"
OPERATION_ID_PREFIX = "OP-"
PAUSE_POLL_SECONDS = 1.0
# Before a measurement phase, wait at most this long for VACUUM/ANALYZE (autovacuum included) on the
# database to finish: a phase measured through maintenance measures the maintenance. A method
# parameter, not an acceptance threshold; what was seen and waited is recorded in the step.
SETTLE_MAX_SECONDS = 180.0
SETTLE_POLL_SECONDS = 2.0

# Kept for importers of the DB-01 names.
STATUS_RETAINED, STATUS_REVERTED, STATUS_REJECTED = st.RETAINED, st.REVERTED, st.REJECTED
STATUS_BLOCKED, STATUS_NO_CANDIDATE, STATUS_CANCELLED = st.BLOCKED, st.NO_CANDIDATE, st.CANCELLED
STATUS_FAILED_RECOVERED, STATUS_FAILED_NEEDS_RECOVERY = st.FAILED_RECOVERED, st.FAILED_NEEDS_RECOVERY
STATUS_RECOVERED_REVERTED = st.RECOVERED_REVERTED


class OperationBlocked(Exception):
    """A precondition failed; nothing was or will be written by this operation."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class OperationCancelled(Exception):
    def __init__(self, where: str):
        super().__init__(f"cancelled by an operator at {where}")
        self.where = where


@dataclass
class OptimizeRequest:
    target: str
    workload_path: str
    candidates: list[str] = field(default_factory=list)
    use_llm: bool = False
    llm_model: str | None = None
    docs_dir: str | None = None
    fault_after: str | None = None
    actor: str = ACTOR_CLI
    request_id: str | None = None


@dataclass
class OperationOutcome:
    operation_id: str
    status: str
    message: str
    decision: dict | None
    evidence_dir: str
    cause: str | None = None


def new_operation_id() -> str:
    return f"{OPERATION_ID_PREFIX}{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _target_queries_for(candidate: Candidate, plans: Measurement, controls: list[str]) -> list[str]:
    """Workload queries whose baseline plan sequentially scans the candidate's table filtering on its leading column."""
    lead = candidate.spec.columns[0]
    hits = []
    for qid, plan in plans.first_plans.items():
        if qid in controls:
            continue
        for scan in plan.seq_scans:
            same_table = scan["relation"] == candidate.spec.table and (scan.get("schema") in (None, candidate.spec.schema))
            if same_table and any(p["column"] == lead for p in scan.get("predicates", [])):
                hits.append(qid)
                break
    return hits


def _medians(series: dict[str, list[float]]) -> dict[str, float]:
    return {q: round(sorted(v)[len(v) // 2], 4) for q, v in series.items() if v}


def state_delta(before: dict, after: dict) -> dict:
    """What a measurement phase did to the lab: dead tuples, rows inserted, identity values consumed, sizes."""
    def by_table(snap):
        return {f"{t['schema']}.{t['table']}": t for t in snap.get("tables", [])}

    tb, ta = by_table(before), by_table(after)
    tables = {}
    for name, a in ta.items():
        b = tb.get(name)
        if not b:
            continue
        tables[name] = {k: (a.get(k) or 0) - (b.get(k) or 0)
                        for k in ("n_dead_tup", "n_tup_ins", "n_tup_upd", "n_tup_del", "total_bytes", "index_bytes",
                                  "autovacuum_count", "autoanalyze_count")}
    sb = {f"{s['schema']}.{s['sequence']}": s.get("last_value") for s in before.get("sequences", [])}
    seqs = {}
    for s in after.get("sequences", []):
        name = f"{s['schema']}.{s['sequence']}"
        if sb.get(name) is not None and s.get("last_value") is not None:
            seqs[name] = int(s["last_value"]) - int(sb[name])
    return {"tables": tables, "sequences_consumed": seqs,
            "database_bytes": (after.get("database_bytes") or 0) - (before.get("database_bytes") or 0)}


class IndexOptimization:
    def __init__(
        self,
        cfg: AppConfig,
        target: TargetConfig,
        adapter,
        policy: Policy,
        workload: Workload,
        request: OptimizeRequest,
        *,
        executor_factory: Callable[[str], Any],
        llm: ag.LLMCall | None = None,
        fault_hook: Callable[[str], None] | None = None,
        operation_id: str | None = None,
        test_hooks: dict[str, Callable[[], None]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.target = target
        self.adapter = adapter
        self.policy = policy
        self.workload = workload
        self.request = request
        self.executor_factory = executor_factory
        self.llm = llm
        self.fault_hook = fault_hook
        self.test_hooks = test_hooks or {}
        self.sleep = sleep
        self.journal = Journal(cfg)
        self.op = operation_id or new_operation_id()
        self.emitter = OperationEmitter(cfg, actor=request.actor, aggregate_type=AGGREGATE_DB_OPERATION, correlation_id=self.op)
        self.edges = ProvenanceEdgeRepository(cfg)
        self.evidence: dict[str, dict] = {}
        self.plan_evidence: dict[str, str] = {}
        self.snapshots: dict[str, dict] = {}
        self.agent_reports: dict[str, Any] = {}
        self.record: dict = {"operation_id": self.op, "target": target.describe(), "request": request.__dict__}
        self.executor: Any = None
        self.change: Candidate | None = None
        self.retained = False
        self.recovery: dict | None = None
        if hasattr(adapter, "bind_operation"):
            adapter.bind_operation(self.op)

    # --- plumbing ------------------------------------------------------------
    def _emit(self, event_type: str, payload: dict) -> None:
        self.emitter.emit(event_type, aggregate_id=self.op, payload=payload)

    def _artifact(self, name: str, kind: str, content: object) -> str:
        return str(self.journal.write_artifact(self.op, name, kind, content))

    def _hook(self, name: str) -> None:
        hook = self.test_hooks.get(name)
        if hook:
            hook()

    def _step(self, name: str, fn: Callable[[], tuple[object, dict]], detail: dict | None = None):
        self._checkpoint(f"before {name}")
        seq = self.journal.step_started(self.op, name, detail)
        started = time.perf_counter()
        try:
            result, summary = fn()
        except (OperationBlocked, OperationCancelled) as exc:
            self.journal.step_finished(self.op, seq, "failed", {"blocked" if isinstance(exc, OperationBlocked) else "cancelled": True,
                                                                "detail": str(exc)})
            raise
        except Exception as exc:
            message = redact(f"{type(exc).__name__}: {exc}")
            self.journal.step_finished(self.op, seq, "failed", {"error": message})
            self._emit(st.EVT_STEP_FAILED, {"step": name, "error": message})
            raise
        summary = {**summary, "seconds": round(time.perf_counter() - started, 3)}
        self.journal.step_finished(self.op, seq, "completed", summary)
        self._emit(st.EVT_STEP_COMPLETED, {"step": name, **summary})
        if self.fault_hook and self.request.fault_after == name:
            self.fault_hook(name)
        return result

    def _add_evidence(self, kind: str, content: dict) -> str:
        eid = f"E{len(self.evidence) + 1}"
        self.evidence[eid] = {"kind": kind, **content}
        return eid

    # --- safe points: heartbeat, pause, cancel ----------------------------------------
    def _checkpoint(self, where: str) -> None:
        if self.executor is not None:
            # A lost lease surfaces at the next executor check, with its own refusal code.
            with contextlib.suppress(Exception):
                self.executor.heartbeat()
        for control in self.journal.pending_controls(self.op):
            action = control["action"]
            if action == "cancel":
                self.journal.acknowledge_control(self.op, control["seq"], f"cancelling at {where}")
                raise OperationCancelled(where)
            if action == "resume":
                self.journal.acknowledge_control(self.op, control["seq"], "ignored: the operation was not paused")
                continue
            self.journal.acknowledge_control(self.op, control["seq"], f"paused at {where}")
            self._pause(where)

    def _pause(self, where: str) -> None:
        self.journal.set_status(self.op, st.PAUSED)
        self._emit(st.EVT_PAUSED, {"at": where, "lease_kept": self.executor is not None})
        last_beat = time.monotonic()
        while True:
            self.sleep(PAUSE_POLL_SECONDS)
            if self.executor is not None and time.monotonic() - last_beat >= 10:
                with contextlib.suppress(Exception):
                    self.executor.heartbeat()
                last_beat = time.monotonic()
            for control in self.journal.pending_controls(self.op):
                if control["action"] == "cancel":
                    self.journal.acknowledge_control(self.op, control["seq"], f"cancelling while paused at {where}")
                    raise OperationCancelled(f"{where} (while paused)")
                if control["action"] == "resume":
                    self.journal.acknowledge_control(self.op, control["seq"], f"resumed at {where}")
                    self.journal.set_status(self.op, st.RUNNING)
                    self._emit(st.EVT_RESUMED, {"at": where})
                    return
                self.journal.acknowledge_control(self.op, control["seq"], "ignored: already paused")

    # --- the operation ---------------------------------------------------------
    def run(self) -> OperationOutcome:
        self.journal.create(
            self.op, KIND_INDEX_OPTIMIZATION, self.target.name, self.request.__dict__, self.policy.sha256, self.workload.sha256
        )
        proc = current_process()
        self.journal.set_runtime(self.op, holder_host=proc["host"], holder_pid=proc["pid"], process_started_at=proc["started_at"])
        # The exact policy and workload in force, so the decision can be recomputed from this bundle alone.
        self._artifact("policy.yaml", "policy", Path(self.policy.source).read_bytes().decode("utf-8"))
        self._artifact("workload.yaml", "workload", Path(self.workload.source).read_bytes().decode("utf-8"))
        self._emit(st.EVT_STARTED, {
            "target": self.target.name, "workload": self.workload.name, "workload_sha256": self.workload.sha256,
            "policy_sha256": self.policy.sha256, "policy_version": self.policy.version, "llm": self.request.use_llm,
        })
        decision: dict | None = None
        try:
            status, cause, message, decision = self._run()
        except OperationBlocked as exc:
            status, cause, message = st.BLOCKED, exc.code, str(exc)
            if self._change_present():
                clean = self._compensate(f"blocked after the change: {exc}")
                status = st.FAILED_RECOVERED if clean else st.FAILED_NEEDS_RECOVERY
        except OperationCancelled as exc:
            cause = st.CAUSE_CANCELLED
            if self.retained:
                # The decision was already applied: a cancel cannot un-decide it. Said, not hidden.
                status, cause = st.RETAINED, st.CAUSE_ACCEPTED
                message = (f"{self.change.spec.key if self.change else 'change'} retained: every acceptance criterion passed; "
                           f"a cancel requested at {exc.where} arrived after the retention and had no effect.")
                current = self.journal.get(self.op)
                decision = current.decision if current else None
            elif self._change_present():
                clean = self._compensate(f"operator cancel at {exc.where}")
                status = st.CANCELLED if clean else st.FAILED_NEEDS_RECOVERY
                message = (f"Cancelled at {exc.where}; the change was removed (compensation confirmed in the catalog)." if clean
                           else f"Cancelled at {exc.where}; the change could not be confirmed removed: recovery needed.")
            else:
                status, message = st.CANCELLED, f"Cancelled at {exc.where}; no change had been made."
        except ExecutionFailed as exc:
            cause = exc.code
            clean = exc.cleaned or self._compensate(f"execution failed: {exc.code}")
            status = st.FAILED_RECOVERED if clean else st.FAILED_NEEDS_RECOVERY
            message = f"{exc.code}: {exc.detail}. " + ("The target was confirmed back to its prior state." if clean
                                                        else "Its change could not be confirmed undone: recovery needed.")
            self.record["execution_failure"] = {"code": exc.code, "detail": exc.detail, "cleanup": exc.cleanup}
            self._emit(st.EVT_FAILED, {"status": status, "cause": cause})
        except ExecutorRefusal as exc:
            cause = exc.code
            if self._change_present():
                clean = self._compensate(f"executor refusal after the change: {exc.code}")
                status = st.FAILED_RECOVERED if clean else st.FAILED_NEEDS_RECOVERY
            else:
                status = st.BLOCKED
            message = f"The executor refused: {exc.code}: {exc.detail}"
        except Exception as exc:  # noqa: BLE001 - any failure must end in an explicit, journaled state
            cause = st.CAUSE_ERROR
            error = redact(f"{type(exc).__name__}: {exc}")
            clean = self._compensate(f"failure: {error}")
            status = st.FAILED_RECOVERED if clean else st.FAILED_NEEDS_RECOVERY
            message = f"{error}. " + ("The target was confirmed back to its prior state." if clean
                                      else "Its change could not be confirmed undone: recovery needed.")
            self._emit(st.EVT_FAILED, {"status": status, "error": error})
        finally:
            if self.executor is not None:
                self.executor.release()
        self.journal.set_runtime(self.op, cause_code=cause)
        self._write_report(status, cause, message, decision)
        self.journal.finish(self.op, status, message, decision)
        self._emit(st.EVT_COMPLETED, {"status": status, "cause": cause, "message": message})
        return OperationOutcome(self.op, status, message, decision, str(evidence_dir(self.cfg, self.op)), cause)

    def _run(self) -> tuple[str, str, str, dict | None]:
        self._step("preflight", self._preflight)
        self._step("discover", self._discover)
        self.snapshots["initial"] = self.adapter.state_snapshot()
        baseline: Measurement = self._step("baseline", lambda: self._measure("baseline", self.policy.baseline_rounds))
        self.snapshots["after_baseline"] = self.adapter.state_snapshot()
        candidates: list[Candidate] = self._step("diagnose", lambda: self._diagnose(baseline))
        if self.request.use_llm:
            extra = self._step("analyze", lambda: self._analyze(candidates, baseline))
            candidates.extend(extra)
        if not candidates:
            return st.NO_CANDIDATE, st.CAUSE_NO_CANDIDATE, "The measured workload offered no index candidate; nothing was changed.", None

        eligible: list[Candidate] = self._step("validate", lambda: self._validate(candidates, baseline))
        if not eligible:
            return st.REJECTED, st.CAUSE_POLICY_REJECTED, "Every candidate was rejected by the policy; nothing was changed.", None
        if self.request.use_llm:
            self._step("review", lambda: self._review(eligible))

        chosen = eligible[0]
        self._hook("before_authorize")
        auth: AuthorizationContext = self._step("authorize", lambda: self._authorize(chosen))
        self._hook("before_apply")
        self.change = chosen
        self._step("apply", lambda: self._apply(chosen, auth))
        self.snapshots["after_change"] = self.adapter.state_snapshot()
        after: Measurement = self._step("measure", lambda: self._measure("after", self.policy.after_rounds))
        self.snapshots["after_measure"] = self.adapter.state_snapshot()
        decision = self._step("decide", lambda: self._decide(chosen, baseline, after))
        if self.request.use_llm:
            self._step("validate_assessment", lambda: self._validator(decision))
            self._step("verify_claims", self._verify_claims)
        if decision["outcome"] == RETAIN:
            self._step("retain", lambda: self._retain())
            status, cause = st.RETAINED, st.CAUSE_ACCEPTED
            message = f"{chosen.spec.key} retained: every acceptance criterion passed."
        else:
            self._step("revert", lambda: self._revert(chosen, "acceptance criteria not met", baseline))
            failed = [c["name"] for c in decision["criteria"] if c["status"] != "pass"]
            status, cause = st.REVERTED, st.CAUSE_ACCEPTANCE_FAILED
            message = f"{chosen.spec.key} reverted (compensation confirmed in the catalog): failed {failed}."
        self._step("verify", lambda: self._verify(chosen, retained=status == st.RETAINED))
        return status, cause, message, decision

    # --- steps -------------------------------------------------------------------
    def _preflight(self):
        identity = self.adapter.identity()
        authority = authorize_write(self.target, identity, self.policy, OP_CREATE_INDEX)
        self.record["identity"] = identity
        self.record["authority"] = authority.describe()
        self._artifact("authority.json", "policy", {"identity": identity, "authority": authority.describe()})
        if not authority.eligible:
            raise OperationBlocked(
                st.CAUSE_AUTHORITY_REFUSED,
                "Write authority refused: " + "; ".join(f"{c.name} ({c.status}: {c.detail})" for c in authority.blocking),
            )
        try:
            executor = self.executor_factory(self.op)
        except ExecutorRefusal as exc:
            raise OperationBlocked(exc.code, f"No executor for this target: {exc.detail}") from None
        try:
            lease = executor.acquire()
        except ExecutorRefusal as exc:
            executor.close()
            raise OperationBlocked(exc.code, f"Target lease unavailable: {exc.detail}") from None
        except ConnectionError as exc:
            executor.close()
            raise OperationBlocked("target_unreachable", f"The operator identity could not connect: {exc}") from None
        self.executor = executor
        self.journal.set_runtime(self.op, lease_token=lease.fencing_token)
        self.record["lease"] = lease.describe()

        from . import recovery

        detection = recovery.detect(self.adapter, self.journal, self.target, self.policy, exclude=(self.op,))
        if not detection["clean"]:
            if self.policy.recovery_automatic and detection["recoverable"]:
                self.recovery = recovery.recover(
                    self.cfg, self.target, self.adapter, self.policy, self.executor_factory,
                    mode=recovery.MODE_AUTOMATIC_PREFLIGHT, actor=self.request.actor,
                    acting_executor=self.executor, acting_operation=self.op, sleep=self.sleep,
                )
                if self.recovery["status"] not in (st.RECOVERY_DONE, st.RECOVERY_NOTHING):
                    raise OperationBlocked(st.CAUSE_UNRECOVERED_STATE,
                                           f"Unfinished state on {self.target.name} could not be recovered automatically "
                                           f"(run {self.recovery['run_id']}): {self.recovery['message']}")
            else:
                why = "automatic recovery is disabled by the policy" if not self.policy.recovery_automatic else "it is not provably recoverable"
                raise OperationBlocked(
                    st.CAUSE_UNRECOVERED_STATE,
                    f"Unfinished state on {self.target.name} ({detection['blocking']} blocking incident(s)) and {why}: "
                    + "; ".join(f"{i['kind']} {i['subject']}" for i in detection["incidents"] if i["severity"] == "blocking")
                    + ". Run `field-horizon db recover` or use the incident view.",
                )
        return identity, {"server": identity.get("version_num"), "authority": "granted", "lease_token": lease.fencing_token,
                          "recovery": self.recovery["run_id"] if self.recovery else None}

    def _discover(self):
        snapshot = self.adapter.discover()
        snapshot["server"] = self.record.get("identity")
        snapshot["target"] = self.target.describe()
        snapshot["findings"] = structural_findings(snapshot)
        recorded = self.journal.record_snapshot(self.target.name, snapshot)
        self.record["snapshot"] = recorded
        self.discovery = snapshot
        self._artifact("snapshot_ref.json", "snapshot", recorded)
        for finding in snapshot["findings"]:
            self._add_evidence("structural_finding", finding)
        for table in snapshot["tables"]:
            self._add_evidence("table", {k: table[k] for k in ("schema", "table", "kind", "estimated_rows", "total_bytes", "column_statistics")})
        for idx in snapshot["indexes"]:
            self._add_evidence("index", {k: idx[k] for k in ("schema", "table", "index", "columns", "is_valid", "idx_scan")})
        for doc in documentary_evidence(self.request.docs_dir):
            self._add_evidence("documentation", doc)
        return snapshot, {"snapshot_id": recorded["snapshot_id"], "findings": len(snapshot["findings"]),
                          "availability": recorded["summary"]["availability"]}

    def _settle(self, label: str) -> dict:
        """Waits (bounded) for running maintenance to end before a measurement phase; returns what it saw."""
        if not hasattr(self.adapter, "maintenance_activity"):
            return {"checked": False}
        started = time.monotonic()
        seen: list[dict] = []
        while True:
            running = self.adapter.maintenance_activity()
            if not running:
                return {"checked": True, "waited_seconds": round(time.monotonic() - started, 1), "seen": seen}
            seen = [{k: r.get(k) for k in ("backend_type", "query", "vacuum_relation", "vacuum_phase", "analyze_relation")}
                    for r in running]
            if time.monotonic() - started >= SETTLE_MAX_SECONDS:
                return {"checked": True, "waited_seconds": round(time.monotonic() - started, 1), "seen": seen,
                        "still_running": True}
            self._checkpoint(f"{label}: waiting for maintenance to finish")
            self.sleep(SETTLE_POLL_SECONDS)

    def _vacuum_before(self, label: str) -> dict:
        """The same starting state for every phase: VACUUM the lab tables holding dead tuples left by measurement writes."""
        if not self.policy.vacuum_before_phase or self.executor is None:
            return {"done": False, "reason": "policy measurement.vacuum_before_phase is off" if self.executor else "no executor"}
        snapshot = self.adapter.state_snapshot()
        dead = {f"{t['schema']}.{t['table']}": t.get("n_dead_tup") or 0 for t in snapshot.get("tables", []) if (t.get("n_dead_tup") or 0) > 0}
        if not dead:
            return {"done": True, "tables": {}}
        auth = self.executor.authorize()
        for name in dead:
            schema, table = name.split(".", 1)
            self.executor.vacuum_table(auth, VacuumTable(schema, table))
        return {"done": True, "tables": dead, "note": f"dead tuples removed before {label}"}

    def _measure(self, label: str, rounds: int, workload: Workload | None = None, warmup: int | None = None):
        workload = workload or self.workload
        settle = self._settle(label) if label in ("baseline", "after") else {"checked": False}
        if label in ("baseline", "after"):
            settle["vacuum"] = self._vacuum_before(label)
        measurement = run_workload(
            self.adapter, workload, rounds=rounds, warmup_runs=self.policy.warmup_runs if warmup is None else warmup, label=label,
            on_round=lambda r: self._checkpoint(f"{label} round {r + 1}/{rounds}") if r else None,
            client_timing=self.policy.client_timing,
        )
        path = self._artifact(f"{label}.json", "measurement", measurement.to_dict())
        medians = _medians(measurement.series())
        if label == "baseline":
            for qid, plan in measurement.first_plans.items():
                self.plan_evidence[qid] = self._add_evidence("baseline_plan", {
                    "query_id": qid, "median_ms": medians.get(qid), "node_types": plan.node_types,
                    "indexes_used": plan.indexes_used, "seq_scans": plan.seq_scans,
                })
        self.record.setdefault("settle", {})[label] = settle
        return measurement, {"samples": len(measurement.samples), "median_ms": medians,
                             "client_median_ms": _medians(measurement.client_series()), "settle": settle, "artifact": path}

    def _diagnose(self, baseline: Measurement):
        controls = self.workload.controls()
        plans = {q: p for q, p in baseline.first_plans.items() if q not in controls}
        candidates, observations = propose_index_candidates(plans)
        for text in self.request.candidates:
            manual = parse_candidate_argument(text)
            manual.target_queries = _target_queries_for(manual, baseline, controls)
            manual.evidence_refs = [f"plan:{q}" for q in manual.target_queries]
            candidates.append(manual)
        for c in candidates:
            c.target_queries = [q for q in c.target_queries if q not in controls]
        self._artifact("candidates.json", "candidates", {"candidates": [c.describe() for c in candidates], "observations": observations,
                                                         "controls_excluded": controls})
        for c in candidates:
            # In the pack, a candidate points at the pack's own ids, never at internal "plan:<query>" refs.
            described = c.describe()
            described["evidence_refs"] = [self.plan_evidence.get(r.removeprefix("plan:"), r) for r in c.evidence_refs]
            self._add_evidence("candidate", described)
            self.edges.append(OBJECT_DB_EVIDENCE, f"{self.op}/baseline.json", OBJECT_DB_CANDIDATE, c.candidate_id,
                              REL_DERIVED_FROM, provenance_ref=self.op)
        return candidates, {"candidates": [c.spec.key for c in candidates], "observations": len(observations)}

    def _analyze(self, candidates: list[Candidate], baseline: Measurement):
        reports = {}
        for role in (ag.ROLE_ARCHITECT, ag.ROLE_DIAGNOSTICIAN, ag.ROLE_PLANNER):
            self._checkpoint(f"analyze ({role})")
            reports[role] = ag.run_role(self.cfg, role, self.evidence, task=f"Workload {self.workload.name} on {self.target.name}.",
                                        model=self.request.llm_model, llm=self.llm)
        self.agent_reports.update(reports)
        extra: list[Candidate] = []
        known = {c.spec.key for c in candidates}
        controls = self.workload.controls()
        for prop in reports[ag.ROLE_PLANNER].proposals:
            try:
                cand = parse_candidate_argument(f"{prop['schema']}.{prop['table']}({', '.join(map(str, prop['columns']))})")
            except ValueError:
                continue
            cand.source = f"agent:{ag.ROLE_PLANNER}"
            cand.rationale = str(prop.get("reason", ""))[:500]
            cand.evidence_refs = [f"evidence:{e}" for e in prop.get("evidence", [])]
            cand.target_queries = _target_queries_for(cand, baseline, controls)
            if cand.spec.key in known:
                continue
            known.add(cand.spec.key)
            extra.append(cand)
        self._artifact("agents_analysis.json", "agents", {"evidence": self.evidence, "reports": {r: rep.describe() for r, rep in reports.items()}})
        return extra, {
            role: {"accepted": len(rep.accepted), "rejected": len(rep.rejected), "proposals": len(rep.proposals), "error": rep.error}
            for role, rep in reports.items()
        }

    def _validate(self, candidates: list[Candidate], baseline: Measurement):
        running = self.adapter.running_transactions()
        free = free_disk_bytes(self.target)
        verdicts = {}
        weights = self.workload.weights()
        medians = {q: sorted(v)[len(v) // 2] for q, v in baseline.series().items()}
        eligible = []
        for c in candidates:
            facts = self.adapter.table_facts(c.spec.schema, c.spec.table)
            stats = self.adapter.column_statistics(c.spec.schema, c.spec.table, list(c.spec.columns)) if facts.get("exists") else {}
            verdict = evaluate_candidate(c, facts, stats, running, self.policy, free)
            verdicts[c.candidate_id] = {**verdict.describe(), "candidate": c.describe(), "table_facts": facts, "column_stats": stats}
            self._emit(st.EVT_CANDIDATE_EVALUATED, {"candidate": c.spec.key, "eligible": verdict.eligible,
                                                    "blocking": [b.name for b in verdict.blocking]})
            self._add_evidence("policy_verdict", {"candidate_id": c.candidate_id, "key": c.spec.key, **verdict.describe()})
            if verdict.eligible:
                eligible.append(c)
        eligible.sort(key=lambda c: -sum(weights.get(q, 0.0) * medians.get(q, 0.0) for q in c.target_queries))
        self.verdicts = verdicts
        self._artifact("policy_verdicts.json", "policy", {"free_disk_bytes": free, "running_transactions": running, "verdicts": verdicts})
        return eligible, {
            "eligible": [c.spec.key for c in eligible],
            "rejected": {verdicts[c.candidate_id]["candidate"]["key"]: verdicts[c.candidate_id]["blocking"]
                         for c in candidates if c not in eligible},
        }

    def _review(self, eligible: list[Candidate]):
        report = ag.run_role(self.cfg, ag.ROLE_REVIEWER, self.evidence,
                             task="Candidates under review: " + ", ".join(f"{c.candidate_id} = {c.spec.key}" for c in eligible),
                             model=self.request.llm_model, llm=self.llm)
        self.agent_reports[ag.ROLE_REVIEWER] = report
        mapped = ag.map_objections(report.objections, self.verdicts)
        self._artifact("agents_review.json", "agents", {"report": report.describe(), "objections": mapped})
        return mapped, {"objections": len(mapped), "rejected": len(report.rejected), "error": report.error}

    def _authorize(self, chosen: Candidate):
        """Immediately before the change: everything that justified it, checked again on fresh facts."""
        assert self.executor is not None
        on_disk = sha256_file(self.policy.source)
        if on_disk != self.policy.sha256:
            raise OperationBlocked("policy_changed", f"the policy file changed since the operation started ({self.policy.sha256[:12]} -> "
                                                     f"{(on_disk or 'unreadable')[:12]}); nothing was changed")
        identity = self.adapter.identity()
        authority = authorize_write(self.target, identity, self.policy, OP_CREATE_INDEX)
        if not authority.eligible:
            raise OperationBlocked(st.CAUSE_AUTHORITY_REFUSED,
                                   "Write authority no longer holds: " + "; ".join(f"{c.name} ({c.detail})" for c in authority.blocking))
        facts = self.adapter.table_facts(chosen.spec.schema, chosen.spec.table)
        stats = self.adapter.column_statistics(chosen.spec.schema, chosen.spec.table, list(chosen.spec.columns)) if facts.get("exists") else {}
        running = self.adapter.running_transactions()
        verdict = evaluate_candidate(chosen, facts, stats, running, self.policy, free_disk_bytes(self.target))
        if not verdict.eligible:
            raise OperationBlocked("not_eligible", f"{chosen.spec.key} is no longer eligible: "
                                   + "; ".join(f"{c.name} ({c.detail})" for c in verdict.blocking))
        auth = self.executor.authorize(eligibility={
            "candidate_key": chosen.spec.key, "table_oid": facts.get("oid"),
            "checks": [{"name": c.name, "status": c.status} for c in verdict.checks],
            "running_transactions": len(running),
        })
        self.authorization = auth
        self._artifact("authorization.json", "policy", {"authorization": auth.describe(), "verdict": verdict.describe(),
                                                        "identity": identity})
        return auth, {"candidate": chosen.spec.key, "verified_at": auth.verified_at, "lease_token": auth.lease.fencing_token,
                      "max_age_seconds": self.policy.max_authorization_age_seconds}

    def _apply(self, chosen: Candidate, auth: AuthorizationContext):
        assert self.executor is not None
        spec = chosen.spec
        self.journal.change_planned(self.op, CHANGE_ID, chosen.describe(), spec.schema, spec.name)
        self.journal.change_state(self.op, CHANGE_ID, "applying")
        started = time.perf_counter()
        state: IndexState = self.executor.create_index(auth, CreateIndex(
            change_id=CHANGE_ID, schema=spec.schema, table=spec.table, columns=spec.columns, index_name=spec.name,
            table_oid=auth.eligibility.get("table_oid"), candidate_key=spec.key,
        ))
        build_seconds = time.perf_counter() - started
        self.journal.change_state(self.op, CHANGE_ID, "applied")
        self.executor.analyze_table(auth, AnalyzeTable(spec.schema, spec.table))
        change = {"index": state.describe(), "build_seconds": round(build_seconds, 3), "lease_token": auth.lease.fencing_token}
        self._artifact("change.json", "change", change)
        self._emit(st.EVT_CHANGE_APPLIED, {"index": f"{spec.schema}.{spec.name}", "size_bytes": state.size_bytes,
                                           "build_seconds": round(build_seconds, 3)})
        return state, {"index": f"{spec.schema}.{spec.name}", "size_bytes": state.size_bytes, "build_seconds": round(build_seconds, 3)}

    def _decide(self, chosen: Candidate, baseline: Measurement, after: Measurement):
        decision = decide(
            baseline.series(), after.series(), self.workload.roles(), self.workload.weights(),
            chosen.target_queries, after.index_usage(chosen.spec.name), self.policy,
            controls=self.workload.controls(), client_baseline=baseline.client_series(), client_after=after.client_series(),
        ).describe()
        decision["candidate"] = chosen.describe()
        decision["policy"] = {"version": self.policy.version, "sha256": self.policy.sha256}
        decision["side_effects"] = {
            "baseline": state_delta(self.snapshots.get("initial", {}), self.snapshots.get("after_baseline", {})),
            "after": state_delta(self.snapshots.get("after_change", {}), self.snapshots.get("after_measure", {})),
        }
        self.journal.record_decision(self.op, decision)
        self._artifact("decision.json", "decision", decision)
        relation = REL_SUPPORTS if decision["outcome"] == RETAIN else REL_OPPOSES
        self.edges.append(OBJECT_DB_EVIDENCE, f"{self.op}/after.json", OBJECT_DB_OPERATION, self.op, relation, provenance_ref=self.op)
        self.edges.append(OBJECT_DB_CANDIDATE, chosen.candidate_id, OBJECT_DB_OPERATION, self.op, REL_DERIVED_FROM, provenance_ref=self.op)
        for qid, q in decision["queries"].items():
            self._add_evidence("measurement", {"query_id": qid, "median_before_ms": q["baseline"]["median"],
                                               "median_after_ms": q["after"]["median"], "ratio": q["median_ratio"],
                                               "p_faster": q["p_faster"], "p_slower": q["p_slower"]})
        self._add_evidence("decision_criteria", {"outcome": decision["outcome"],
                                                 "criteria": [{k: c[k] for k in ("name", "status")} for c in decision["criteria"]]})
        self._emit(st.EVT_DECISION_RECORDED, {"outcome": decision["outcome"],
                                              "failed": [c["name"] for c in decision["criteria"] if c["status"] != "pass"]})
        return decision, {"outcome": decision["outcome"], "weighted_ratio": round(decision["weighted"]["ratio"], 4)}

    def _validator(self, decision: dict):
        report = ag.run_role(self.cfg, ag.ROLE_VALIDATOR, self.evidence, task="Assess the measured change.",
                             model=self.request.llm_model, llm=self.llm)
        self.agent_reports[ag.ROLE_VALIDATOR] = report
        deterministic = "retain" if decision["outcome"] == RETAIN else "revert"
        agreement = None if report.assessment is None else report.assessment == deterministic
        self._artifact("agents_validation.json", "agents", {"report": report.describe(), "deterministic_outcome": deterministic,
                                                            "agrees": agreement, "effect": "none: recorded for audit only"})
        return agreement, {"assessment": report.assessment, "agrees_with_policy": agreement, "error": report.error}

    def _verify_claims(self):
        verification = verify_agent_reports(
            {role: rep.describe() for role, rep in self.agent_reports.items()}, self.evidence, getattr(self, "discovery", {}),
        )
        self._artifact("claims.json", "claims", verification)
        return verification, verification["summary"]

    def _retain(self):
        assert self.executor is not None
        auth = self.executor.authorize(eligibility={"candidate_key": self.change.spec.key if self.change else None})
        self.executor.mark_retained(auth, CHANGE_ID)
        self.retained = True
        self.journal.change_state(self.op, CHANGE_ID, "retained")
        return True, {"ledger": "retained"}

    def _revert(self, chosen: Candidate, reason: str, baseline: Measurement):
        assert self.executor is not None
        self.journal.change_state(self.op, CHANGE_ID, "reverting")
        auth = self.executor.authorize()
        result = self.executor.drop_own_index(auth, DropOwnIndex(self.op, CHANGE_ID, chosen.spec.schema, chosen.spec.name, reason))
        self.journal.change_state(self.op, CHANGE_ID, "reverted")
        self._emit(st.EVT_CHANGE_REVERTED, {"index": f"{chosen.spec.schema}.{chosen.spec.name}", "reason": reason, "kind": "compensation"})
        check = self._post_compensation_check(chosen, baseline)
        self._artifact("compensation.json", "change", {"drop": result, "post_check": check})
        return result, {"index": f"{chosen.spec.schema}.{chosen.spec.name}", "dropped": result.get("dropped"),
                        "catalog_confirms_absence": check["catalog"]["index_absent"], "post_check_ratio": check["target_ratio"]}

    def _post_compensation_check(self, chosen: Candidate, baseline: Measurement) -> dict:
        """After removing a change: the catalog shows it gone, and the target queries are back to their baseline plans."""
        state = self.adapter.index_state(chosen.spec.schema, chosen.spec.name)
        ledger = [r for r in self.adapter.ledger_rows() if r["operation_id"] == self.op] if hasattr(self.adapter, "ledger_rows") else []
        catalog = {"index_absent": state is None, "ledger_states": [r["state"] for r in ledger],
                   "invalid_indexes": self.adapter.invalid_indexes() if hasattr(self.adapter, "invalid_indexes") else []}
        targets = [q for q in self.workload.queries if q.query_id in chosen.target_queries]
        ratio = None
        if targets:
            sub = Workload(name=f"{self.workload.name}:post_compensation", version=self.workload.version, sha256=self.workload.sha256,
                           source=self.workload.source, queries=tuple(WorkloadQuery(**{**q.__dict__, "repeat": 1}) for q in targets))
            post = run_workload(self.adapter, sub, rounds=1, warmup_runs=0, label="post_compensation")
            base = _medians(baseline.series())
            now = _medians(post.series())
            ratios = [now[q] / base[q] for q in now if base.get(q)]
            ratio = round(sorted(ratios)[len(ratios) // 2], 4) if ratios else None
            self._artifact("post_compensation.json", "measurement", post.to_dict())
            catalog["plans_use_removed_index"] = any(chosen.spec.name in s.indexes_used for s in post.samples)
        return {"catalog": catalog, "target_ratio": ratio,
                "note": "informative: confirms the compensation in the catalog and in the plans; not an acceptance criterion"}

    def _verify(self, chosen: Candidate, retained: bool):
        states = self.adapter.fieldhorizon_indexes()
        ours = [s for s in states if s.name == chosen.spec.name and s.schema == chosen.spec.schema]
        invalid = [f"{s.schema}.{s.name}" for s in states if not s.valid]
        ledger = [r for r in self.adapter.ledger_rows() if r["operation_id"] == self.op] if hasattr(self.adapter, "ledger_rows") else []
        ledger_ok = all(r["state"] == ("retained" if retained else "reverted") for r in ledger)
        consistent = ((len(ours) == 1 and ours[0].valid) if retained else not ours) and ledger_ok
        final = {"retained": retained, "index_present": bool(ours), "invalid_fieldhorizon_indexes": invalid,
                 "ledger": ledger, "fieldhorizon_indexes": [s.describe() for s in states]}
        self._artifact("final_state.json", "state", final)
        if not consistent or invalid:
            raise RuntimeError(f"catalog does not match the decision: {final}")
        return final, {"consistent": True, "index_present": bool(ours), "ledger": [r["state"] for r in ledger]}

    # --- failure handling ------------------------------------------------------------
    def _change_present(self) -> bool:
        """Did this operation's change reach the target? The ledger intent precedes every DDL; unknown counts as yes."""
        if self.change is None:
            return False
        if self.executor is None:
            return True
        try:
            row = self.executor.ledger_row(self.op, CHANGE_ID)
        except Exception:  # noqa: BLE001 - cannot tell: assume it did, so it gets compensated or recovered
            return True
        return row is not None and row["state"] != "reverted"

    def _compensate(self, reason: str) -> bool:
        """After a failure or a cancel: remove this operation's change if it reached the target. True if confirmed clean."""
        if self.change is None:
            return True
        spec = self.change.spec
        if self.executor is None:
            return False
        try:
            row = self.executor.ledger_row(self.op, CHANGE_ID)
            if row is None:
                # The ledger intent precedes every DDL: no row, no change.
                self.journal.change_state(self.op, CHANGE_ID, "reverted")
                return True
            if row["state"] == "reverted":
                self.journal.change_state(self.op, CHANGE_ID, "reverted")
                return True
            if row["state"] == "retained":
                return True
            self.journal.change_state(self.op, CHANGE_ID, "reverting")
            auth = self.executor.authorize()
            self.executor.drop_own_index(auth, DropOwnIndex(self.op, CHANGE_ID, spec.schema, spec.name, reason))
            self.journal.change_state(self.op, CHANGE_ID, "reverted")
            self._emit(st.EVT_CHANGE_REVERTED, {"index": f"{spec.schema}.{spec.name}", "reason": reason, "kind": "compensation"})
            return True
        except Exception as exc:  # noqa: BLE001 - reported as FAILED_NEEDS_RECOVERY with this detail
            self.record["compensation_failure"] = redact(f"{type(exc).__name__}: {exc}")
            return False

    # --- report --------------------------------------------------------------------
    def _write_report(self, status: str, cause: str | None, message: str, decision: dict | None) -> None:
        steps = self.journal.steps(self.op)
        artifacts = self.journal.artifacts(self.op)
        controls = self.journal.controls(self.op)
        lines = [
            f"# DB engineering operation {self.op}",
            "",
            f"- **Status:** {status} (cause `{cause}`)",
            f"- **Message:** {message}",
            f"- **Target:** {self.target.name} ({self.target.engine} {self.target.host}:{self.target.port}/{self.target.dbname}, "
            f"environment `{self.target.environment}`, data `{self.target.data_classification}`)",
            f"- **Workload:** {self.workload.name} v{self.workload.version} (sha256 `{self.workload.sha256[:16]}...`)",
            f"- **Policy:** v{self.policy.version} (sha256 `{self.policy.sha256[:16]}...`)",
            f"- **Lease:** token {self.record.get('lease', {}).get('fencing_token')} (target-side, shared by every Field Horizon process)",
            f"- **LLM agents:** {'on, model ' + (self.request.llm_model or self.cfg.default_model) if self.request.use_llm else 'off'}"
            " (advisory only: no agent output is an input to a decision)",
        ]
        if self.recovery:
            lines.append(f"- **Automatic recovery before this operation:** run {self.recovery['run_id']} -- {self.recovery['status']}: "
                         f"{self.recovery['message']}")
        if controls:
            lines += ["", "## Operator controls", "", "| # | Action | By | Requested | Effect |", "|---|---|---|---|---|"]
            for c in controls:
                lines.append(f"| {c['seq']} | {c['action']} | {c['requested_by']} | {c['requested_at']} | {c.get('effect') or 'pending'} |")
        lines += ["", "## Decision chain", "", "| # | Step | Status | Summary |", "|---|---|---|---|"]
        for s in steps:
            detail = {k: v for k, v in s["detail"].items() if k not in ("artifact",)}
            lines.append(f"| {s['seq']} | {s['step']} | {s['status']} | `{str(detail)[:220]}` |")
        if decision:
            lines += ["", "## Measurements", "",
                      "Server time is PostgreSQL's own execution time (EXPLAIN ANALYZE); client time is the plain statement timed "
                      "by the client, reported beside it. The decision uses server time.", "",
                      "| Query | Kind | Weight | Server before [p25-p75] | Server after [p25-p75] | Ratio | 95% CI | p (faster) "
                      "| p (slower) | Client ratio |",
                      "|---|---|---|---|---|---|---|---|---|---|"]
            for qid, q in decision["queries"].items():
                b, a = q["baseline"], q["after"]
                client = q.get("client", {}).get("median_ratio")
                kind = q["role"] + (" (control)" if q.get("control") else "")
                lines.append(
                    f"| {qid} | {kind} | {q['weight']} | {b['median']:.3f} [{b['p25']:.3f}-{b['p75']:.3f}] | "
                    f"{a['median']:.3f} [{a['p25']:.3f}-{a['p75']:.3f}] | {q['median_ratio']:.3f} | "
                    f"[{q['ratio_ci95'][0]:.3f}, {q['ratio_ci95'][1]:.3f}] | {q['p_faster']:.2e} | {q['p_slower']:.2e} | "
                    f"{f'{client:.3f}' if client is not None else 'n/a'} |"
                )
            w = decision["weighted"]
            lines += ["", f"Weighted cost (sum of weight x median server ms; a cost, not a throughput): "
                          f"{w['before']:.3f} -> {w['after']:.3f} (ratio {w['ratio']:.3f})", "",
                      "## Acceptance criteria", "", "| Criterion | Status | Detail |", "|---|---|---|"]
            for c in decision["criteria"]:
                lines.append(f"| {c['name']} | {c['status']} | {c['detail']} |")
            lines += ["", f"**Outcome: {decision['outcome']}**"]
            settle = self.record.get("settle", {})
            vacuumed = {phase: v.get("vacuum", {}).get("tables") for phase, v in settle.items() if v.get("vacuum", {}).get("tables")}
            if vacuumed:
                lines += ["", "## Same starting state for each phase", "",
                          f"VACUUM before each measurement phase removed the dead tuples left by rolled-back measurement writes: {vacuumed}"]
            if any(v.get("seen") for v in settle.values()):
                lines += ["", "## Measurement environment", ""]
                for phase, v in settle.items():
                    lines.append(f"- {phase}: waited {v.get('waited_seconds')}s for maintenance {v.get('seen')}"
                                 + (" -- still running when measurement started" if v.get("still_running") else ""))
            fx = decision.get("side_effects", {})
            if fx:
                lines += ["", "## Side effects of measurement (rolled-back writes still leave traces)", ""]
                for phase, delta in fx.items():
                    lines.append(f"- {phase}: dead tuples {({k: v['n_dead_tup'] for k, v in delta.get('tables', {}).items() if v.get('n_dead_tup')})}, "
                                 f"identity values consumed {delta.get('sequences_consumed')}, database bytes {delta.get('database_bytes'):+d}")
        lines += ["", "## Evidence files", "", "| File | Kind | Bytes | sha256 |", "|---|---|---|---|"]
        for a in artifacts:
            lines.append(f"| {a['name']} | {a['kind']} | {a['bytes']} | `{a['sha256']}` |")
        self._artifact("report.md", "report", "\n".join(lines) + "\n")
        self._artifact("operation.json", "record", {**self.record, "status": status, "cause": cause, "message": message,
                                                     "steps": steps, "changes": self.journal.changes(self.op), "controls": controls,
                                                     "snapshots": self.snapshots, "recovery": self.recovery})


def operation_detail(cfg: AppConfig, operation_id: str) -> dict | None:
    journal = Journal(cfg)
    record = journal.get(operation_id)
    if record is None:
        return None
    return {
        "operation": record.__dict__,
        "runtime": journal.runtime(operation_id),
        "steps": journal.steps(operation_id),
        "changes": journal.changes(operation_id),
        "controls": journal.controls(operation_id),
        "artifacts": journal.artifacts(operation_id),
    }
