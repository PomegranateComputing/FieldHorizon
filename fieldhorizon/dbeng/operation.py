"""
Autonomous index optimization: one authorized operation, end to end,
without a human approving intermediate steps -- and without any step
deciding its own success.

    preflight  -> authority (policy.authorize_write) and no unrecovered changes
    discover   -> versioned snapshot + deterministic structural findings
    baseline   -> workload measured before any change (A/A halves for noise)
    diagnose   -> index candidates from the measured plans (+ operator ones)
    analyze    -> LLM roles over the evidence (optional; advisory only)
    validate   -> policy.evaluate_candidate for every candidate
    apply      -> CREATE INDEX CONCURRENTLY (autocommit) + ANALYZE
    measure    -> the same workload again
    decide     -> policy.decide: RETAIN only if every criterion passes
    revert     -> DROP INDEX CONCURRENTLY of our own index when not retained
    verify     -> catalog matches the decision; no invalid index left
    report     -> evidence bundle + markdown report, all hashed in the journal

Every step is journaled before and after it runs, every change is in a
ledger with its undo, and `recover` brings any interrupted operation to an
explicit terminal state. A failure is never a silent success: it ends as
FAILED_RECOVERED (our change undone) or FAILED_NEEDS_RECOVERY.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import AppConfig
from ..events import ACTOR_CLI, OperationEmitter
from ..provenance import REL_DERIVED_FROM, REL_OPPOSES, REL_SUPPORTS, ProvenanceEdgeRepository
from . import agents as ag
from .advisor import parse_candidate_argument, propose_index_candidates, structural_findings
from .docs import documentary_evidence
from .journal import Journal, evidence_dir
from .model import Candidate, IndexState
from .policy import RETAIN, Policy, authorize_write, decide, evaluate_candidate, free_disk_bytes
from .targets import TargetConfig, redact
from .workload import Measurement, Workload, run_workload

KIND_INDEX_OPTIMIZATION = "index_optimization"
AGGREGATE_DB_OPERATION = "db_operation"
OBJECT_DB_OPERATION = "db_operation"
OBJECT_DB_CANDIDATE = "db_candidate"
OBJECT_DB_EVIDENCE = "db_evidence"

EVT_DB_OPERATION_STARTED = "DbOperationStarted"
EVT_DB_STEP_COMPLETED = "DbStepCompleted"
EVT_DB_STEP_FAILED = "DbStepFailed"
EVT_DB_CANDIDATE_EVALUATED = "DbCandidateEvaluated"
EVT_DB_CHANGE_APPLIED = "DbChangeApplied"
EVT_DB_DECISION_RECORDED = "DbDecisionRecorded"
EVT_DB_CHANGE_REVERTED = "DbChangeReverted"
EVT_DB_OPERATION_COMPLETED = "DbOperationCompleted"
EVT_DB_OPERATION_FAILED = "DbOperationFailed"
EVT_DB_OPERATION_RECOVERED = "DbOperationRecovered"

STATUS_RETAINED = "RETAINED"
STATUS_REVERTED = "REVERTED"
STATUS_REJECTED = "REJECTED"
STATUS_BLOCKED = "BLOCKED"
STATUS_NO_CANDIDATE = "NO_CANDIDATE"
STATUS_FAILED_RECOVERED = "FAILED_RECOVERED"
STATUS_FAILED_NEEDS_RECOVERY = "FAILED_NEEDS_RECOVERY"
STATUS_RECOVERED_REVERTED = "RECOVERED_REVERTED"

OPERATION_ID_PREFIX = "OP-"


class OperationBlocked(Exception):
    """A precondition failed; nothing was or will be written by this operation."""


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


@dataclass
class OperationOutcome:
    operation_id: str
    status: str
    message: str
    decision: dict | None
    evidence_dir: str


def new_operation_id() -> str:
    return f"{OPERATION_ID_PREFIX}{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _ddl_text(c: Candidate) -> str:
    cols = ", ".join(f'"{col}"' for col in c.spec.columns)
    return f'CREATE INDEX CONCURRENTLY "{c.spec.name}" ON "{c.spec.schema}"."{c.spec.table}" USING {c.spec.method} ({cols})'


def _target_queries_for(candidate: Candidate, plans: Measurement) -> list[str]:
    """Workload queries whose baseline plan sequentially scans the candidate's table filtering on its leading column."""
    lead = candidate.spec.columns[0]
    hits = []
    for qid, plan in plans.first_plans.items():
        for scan in plan.seq_scans:
            same_table = scan["relation"] == candidate.spec.table and (scan.get("schema") in (None, candidate.spec.schema))
            if same_table and any(p["column"] == lead for p in scan.get("predicates", [])):
                hits.append(qid)
                break
    return hits


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
        llm: ag.LLMCall | None = None,
        fault_hook: Callable[[str], None] | None = None,
        operation_id: str | None = None,
    ):
        self.cfg = cfg
        self.target = target
        self.adapter = adapter
        self.policy = policy
        self.workload = workload
        self.request = request
        self.llm = llm
        self.fault_hook = fault_hook
        self.journal = Journal(cfg)
        self.op = operation_id or new_operation_id()
        self.emitter = OperationEmitter(cfg, actor=request.actor, aggregate_type=AGGREGATE_DB_OPERATION, correlation_id=self.op)
        self.edges = ProvenanceEdgeRepository(cfg)
        self.evidence: dict[str, dict] = {}
        self.plan_evidence: dict[str, str] = {}
        self.record: dict = {"operation_id": self.op, "target": target.describe(), "request": request.__dict__}

    # --- plumbing ------------------------------------------------------------
    def _emit(self, event_type: str, payload: dict) -> None:
        self.emitter.emit(event_type, aggregate_id=self.op, payload=payload)

    def _artifact(self, name: str, kind: str, content: object) -> str:
        path = self.journal.write_artifact(self.op, name, kind, content)
        return str(path)

    def _step(self, name: str, fn: Callable[[], tuple[object, dict]], detail: dict | None = None):
        seq = self.journal.step_started(self.op, name, detail)
        started = time.perf_counter()
        try:
            result, summary = fn()
        except OperationBlocked:
            self.journal.step_finished(self.op, seq, "failed", {"blocked": True})
            raise
        except Exception as exc:
            message = redact(f"{type(exc).__name__}: {exc}")
            self.journal.step_finished(self.op, seq, "failed", {"error": message})
            self._emit(EVT_DB_STEP_FAILED, {"step": name, "error": message})
            raise
        summary = {**summary, "seconds": round(time.perf_counter() - started, 3)}
        self.journal.step_finished(self.op, seq, "completed", summary)
        self._emit(EVT_DB_STEP_COMPLETED, {"step": name, **summary})
        if self.fault_hook and self.request.fault_after == name:
            self.fault_hook(name)
        return result

    def _add_evidence(self, kind: str, content: dict) -> str:
        eid = f"E{len(self.evidence) + 1}"
        self.evidence[eid] = {"kind": kind, **content}
        return eid

    # --- the operation ---------------------------------------------------------
    def run(self) -> OperationOutcome:
        self.journal.create(
            self.op, KIND_INDEX_OPTIMIZATION, self.target.name, self.request.__dict__, self.policy.sha256, self.workload.sha256
        )
        self._emit(EVT_DB_OPERATION_STARTED, {
            "target": self.target.name, "workload": self.workload.name, "workload_sha256": self.workload.sha256,
            "policy_sha256": self.policy.sha256, "llm": self.request.use_llm,
        })
        self.change: Candidate | None = None
        try:
            status, message, decision = self._run()
        except OperationBlocked as exc:
            status, message, decision = STATUS_BLOCKED, str(exc), None
        except Exception as exc:
            error = redact(f"{type(exc).__name__}: {exc}")
            recovered = self._undo_own_changes(reason=f"failure: {error}")
            status = STATUS_FAILED_RECOVERED if recovered else STATUS_FAILED_NEEDS_RECOVERY
            message = f"{error}. " + (
                "This operation's change was undone." if recovered
                else "Its change could not be confirmed undone: run `field-horizon db recover`."
            )
            decision = None
            self._emit(EVT_DB_OPERATION_FAILED, {"status": status, "error": error})
        self._write_report(status, message, decision)
        self.journal.finish(self.op, status, message, decision)
        self._emit(EVT_DB_OPERATION_COMPLETED, {"status": status, "message": message})
        return OperationOutcome(self.op, status, message, decision, str(evidence_dir(self.cfg, self.op)))

    def _run(self) -> tuple[str, str, dict | None]:
        identity = self._step("preflight", self._preflight)
        self._step("discover", lambda: self._discover(identity))
        baseline: Measurement = self._step("baseline", lambda: self._measure("baseline", self.policy.baseline_rounds))
        candidates: list[Candidate] = self._step("diagnose", lambda: self._diagnose(baseline))
        if self.request.use_llm:
            extra = self._step("analyze", lambda: self._analyze(candidates, baseline))
            candidates.extend(extra)
        if not candidates:
            return STATUS_NO_CANDIDATE, "The measured workload offered no index candidate.", None

        eligible: list[Candidate] = self._step("validate", lambda: self._validate(candidates, baseline))
        if not eligible:
            return STATUS_REJECTED, "Every candidate was rejected by the policy; nothing was changed.", None
        if self.request.use_llm:
            self._step("review", lambda: self._review(eligible))

        chosen = eligible[0]
        self.change = chosen
        self._step("apply", lambda: self._apply(chosen))
        after: Measurement = self._step("measure", lambda: self._measure("after", self.policy.after_rounds))
        decision = self._step("decide", lambda: self._decide(chosen, baseline, after))
        if self.request.use_llm:
            self._step("validate_assessment", lambda: self._validator(decision))
        if decision["outcome"] == RETAIN:
            self.journal.change_state(self.op, "CH-1", "retained")
            status, message = STATUS_RETAINED, f"{chosen.spec.key} retained: every acceptance criterion passed."
        else:
            self._step("revert", lambda: self._revert(chosen, "acceptance criteria not met"))
            failed = [c["name"] for c in decision["criteria"] if c["status"] != "pass"]
            status, message = STATUS_REVERTED, f"{chosen.spec.key} reverted: failed {failed}."
        self._step("verify", lambda: self._verify(chosen, retained=status == STATUS_RETAINED))
        return status, message, decision

    # --- steps -------------------------------------------------------------------
    def _preflight(self):
        identity = self.adapter.identity()
        authority = authorize_write(self.target, identity, self.policy, "create_index_concurrently")
        self.record["identity"] = identity
        self.record["authority"] = authority.describe()
        self._artifact("authority.json", "policy", {"identity": identity, "authority": authority.describe()})

        leftovers = []
        for state in self.adapter.fieldhorizon_indexes():
            owner_op = (state.marker or {}).get("operation_id")
            owner = self.journal.get(owner_op) if owner_op else None
            if not state.valid or owner is None or owner.status != STATUS_RETAINED:
                leftovers.append({"index": f"{state.schema}.{state.name}", "valid": state.valid, "operation_id": owner_op,
                                  "operation_status": owner.status if owner else None})
        unfinished = [o.operation_id for o in self.journal.unfinished() if o.target == self.target.name and o.operation_id != self.op]
        if leftovers or unfinished:
            raise OperationBlocked(
                f"Unrecovered state on {self.target.name}: indexes {leftovers}, unfinished operations {unfinished}. "
                "Run `field-horizon db recover` first."
            )
        if not authority.eligible:
            raise OperationBlocked(
                "Write authority refused: " + "; ".join(f"{c.name} ({c.status}: {c.detail})" for c in authority.blocking)
            )
        return identity, {"server": identity.get("version_num"), "authority": "granted"}

    def _discover(self, identity: dict):
        snapshot = self.adapter.discover()
        snapshot["server"] = identity
        snapshot["target"] = self.target.describe()
        snapshot["findings"] = structural_findings(snapshot)
        recorded = self.journal.record_snapshot(self.target.name, snapshot)
        self.record["snapshot"] = recorded
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

    def _measure(self, label: str, rounds: int):
        measurement = run_workload(self.adapter, self.workload, rounds=rounds, warmup_runs=self.policy.warmup_runs, label=label)
        path = self._artifact(f"{label}.json", "measurement", measurement.to_dict())
        medians = {q: round(sorted(v)[len(v) // 2], 3) for q, v in measurement.series().items()}
        if label == "baseline":
            for qid, plan in measurement.first_plans.items():
                self.plan_evidence[qid] = self._add_evidence("baseline_plan", {
                    "query_id": qid, "median_ms": medians.get(qid), "node_types": plan.node_types,
                    "indexes_used": plan.indexes_used, "seq_scans": plan.seq_scans,
                })
        return measurement, {"samples": len(measurement.samples), "median_ms": medians, "artifact": path}

    def _diagnose(self, baseline: Measurement):
        candidates, observations = propose_index_candidates(baseline.first_plans)
        for text in self.request.candidates:
            manual = parse_candidate_argument(text)
            manual.target_queries = _target_queries_for(manual, baseline)
            manual.evidence_refs = [f"plan:{q}" for q in manual.target_queries]
            candidates.append(manual)
        self._artifact("candidates.json", "candidates", {"candidates": [c.describe() for c in candidates], "observations": observations})
        for c in candidates:
            # In the pack, a candidate points at the pack's own ids, never at internal "plan:<query>" refs
            # an agent would copy and then (rightly) have rejected as unknown.
            described = c.describe()
            described["evidence_refs"] = [self.plan_evidence.get(r.removeprefix("plan:"), r) for r in c.evidence_refs]
            self._add_evidence("candidate", described)
            self.edges.append(OBJECT_DB_EVIDENCE, f"{self.op}/baseline.json", OBJECT_DB_CANDIDATE, c.candidate_id,
                              REL_DERIVED_FROM, provenance_ref=self.op)
        return candidates, {"candidates": [c.spec.key for c in candidates], "observations": len(observations)}

    def _analyze(self, candidates: list[Candidate], baseline: Measurement):
        reports = {}
        for role in (ag.ROLE_ARCHITECT, ag.ROLE_DIAGNOSTICIAN, ag.ROLE_PLANNER):
            reports[role] = ag.run_role(self.cfg, role, self.evidence, task=f"Workload {self.workload.name} on {self.target.name}.",
                                        model=self.request.llm_model, llm=self.llm)
        extra: list[Candidate] = []
        known = {c.spec.key for c in candidates}
        for prop in reports[ag.ROLE_PLANNER].proposals:
            try:
                cand = parse_candidate_argument(f"{prop['schema']}.{prop['table']}({', '.join(map(str, prop['columns']))})")
            except ValueError:
                continue
            cand.source = f"agent:{ag.ROLE_PLANNER}"
            cand.rationale = str(prop.get("reason", ""))[:500]
            cand.evidence_refs = [f"evidence:{e}" for e in prop.get("evidence", [])]
            cand.target_queries = _target_queries_for(cand, baseline)
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
            self._emit(EVT_DB_CANDIDATE_EVALUATED, {"candidate": c.spec.key, "eligible": verdict.eligible,
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
        mapped = ag.map_objections(report.objections, self.verdicts)
        self._artifact("agents_review.json", "agents", {"report": report.describe(), "objections": mapped})
        return mapped, {"objections": len(mapped), "rejected": len(report.rejected), "error": report.error}

    def _apply(self, chosen: Candidate):
        spec = chosen.spec
        self.journal.change_planned(self.op, "CH-1", chosen.describe(), spec.schema, spec.name)
        existing = self.adapter.index_state(spec.schema, spec.name)
        dropped_invalid = False
        if existing is not None:
            if existing.valid and (existing.marker or {}).get("operation_id") == self.op:
                return existing, {"index": spec.name, "reused": True}
            if not existing.valid and existing.marker:
                # An interrupted earlier build of the same candidate (see pg-createindex-invalid-index-recovery).
                self.adapter.drop_index_concurrently(spec.schema, spec.name, lock_timeout_ms=self.policy.lock_timeout_ms)
                dropped_invalid = True
            else:
                raise OperationBlocked(f"{spec.schema}.{spec.name} already exists and is not this operation's; refusing to touch it.")
        self.journal.change_state(self.op, "CH-1", "applying")
        started = time.perf_counter()
        state: IndexState = self.adapter.create_index_concurrently(
            spec, {"operation_id": self.op, "candidate": spec.key},
            lock_timeout_ms=self.policy.lock_timeout_ms, statement_timeout_ms=self.policy.statement_timeout_ms,
        )
        build_seconds = time.perf_counter() - started
        if not state.valid:
            raise RuntimeError(f"{spec.schema}.{spec.name} was built but is not valid")
        self.journal.change_state(self.op, "CH-1", "applied")
        self.adapter.analyze_table(spec.schema, spec.table)
        change = {"ddl": _ddl_text(chosen), "index": state.__dict__, "build_seconds": round(build_seconds, 3),
                  "dropped_invalid_leftover": dropped_invalid}
        self._artifact("change.json", "change", change)
        self._emit(EVT_DB_CHANGE_APPLIED, {"index": f"{spec.schema}.{spec.name}", "size_bytes": state.size_bytes,
                                           "build_seconds": round(build_seconds, 3)})
        return state, {"index": f"{spec.schema}.{spec.name}", "size_bytes": state.size_bytes, "build_seconds": round(build_seconds, 3)}

    def _decide(self, chosen: Candidate, baseline: Measurement, after: Measurement):
        decision = decide(
            baseline.series(), after.series(), self.workload.roles(), self.workload.weights(),
            chosen.target_queries, after.index_usage(chosen.spec.name), self.policy,
        ).describe()
        decision["candidate"] = chosen.describe()
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
        self._emit(EVT_DB_DECISION_RECORDED, {"outcome": decision["outcome"],
                                              "failed": [c["name"] for c in decision["criteria"] if c["status"] != "pass"]})
        return decision, {"outcome": decision["outcome"], "weighted_ratio": round(decision["weighted"]["ratio"], 4)}

    def _validator(self, decision: dict):
        report = ag.run_role(self.cfg, ag.ROLE_VALIDATOR, self.evidence, task="Assess the measured change.",
                             model=self.request.llm_model, llm=self.llm)
        deterministic = "retain" if decision["outcome"] == RETAIN else "revert"
        agreement = None if report.assessment is None else report.assessment == deterministic
        self._artifact("agents_validation.json", "agents", {"report": report.describe(), "deterministic_outcome": deterministic,
                                                            "agrees": agreement, "effect": "none: recorded for audit only"})
        return agreement, {"assessment": report.assessment, "agrees_with_policy": agreement, "error": report.error}

    def _revert(self, chosen: Candidate, reason: str):
        self.journal.change_state(self.op, "CH-1", "reverting")
        gone = self.adapter.drop_index_concurrently(chosen.spec.schema, chosen.spec.name, lock_timeout_ms=self.policy.lock_timeout_ms)
        if not gone:
            raise RuntimeError(f"{chosen.spec.schema}.{chosen.spec.name} still present after DROP INDEX CONCURRENTLY")
        self.journal.change_state(self.op, "CH-1", "reverted")
        self._emit(EVT_DB_CHANGE_REVERTED, {"index": f"{chosen.spec.schema}.{chosen.spec.name}", "reason": reason})
        return True, {"index": f"{chosen.spec.schema}.{chosen.spec.name}", "dropped": True}

    def _verify(self, chosen: Candidate, retained: bool):
        states = self.adapter.fieldhorizon_indexes()
        ours = [s for s in states if s.name == chosen.spec.name and s.schema == chosen.spec.schema]
        invalid = [f"{s.schema}.{s.name}" for s in states if not s.valid]
        consistent = (len(ours) == 1 and ours[0].valid) if retained else not ours
        final = {"retained": retained, "index_present": bool(ours), "invalid_fieldhorizon_indexes": invalid,
                 "fieldhorizon_indexes": [s.__dict__ for s in states]}
        self._artifact("final_state.json", "state", final)
        if not consistent or invalid:
            raise RuntimeError(f"catalog does not match the decision: {final}")
        return final, {"consistent": True, "index_present": bool(ours)}

    def _undo_own_changes(self, reason: str) -> bool:
        """Best effort, after a failure inside this process: drop whatever this operation created."""
        if self.change is None:
            return True
        spec = self.change.spec
        try:
            state = self.adapter.index_state(spec.schema, spec.name)
            if state is not None and (state.marker or {}).get("operation_id") in (self.op, None):
                self.journal.change_state(self.op, "CH-1", "reverting")
                self.adapter.drop_index_concurrently(spec.schema, spec.name, lock_timeout_ms=self.policy.lock_timeout_ms)
            if self.adapter.index_state(spec.schema, spec.name) is None:
                self.journal.change_state(self.op, "CH-1", "reverted")
                self._emit(EVT_DB_CHANGE_REVERTED, {"index": f"{spec.schema}.{spec.name}", "reason": reason})
                return True
        except Exception:
            return False
        return False

    # --- report --------------------------------------------------------------------
    def _write_report(self, status: str, message: str, decision: dict | None) -> None:
        steps = self.journal.steps(self.op)
        artifacts = self.journal.artifacts(self.op)
        lines = [
            f"# DB engineering operation {self.op}",
            "",
            f"- **Status:** {status}",
            f"- **Message:** {message}",
            f"- **Target:** {self.target.name} ({self.target.engine} {self.target.host}:{self.target.port}/{self.target.dbname}, "
            f"environment `{self.target.environment}`)",
            f"- **Workload:** {self.workload.name} (`{self.workload.source}`, sha256 `{self.workload.sha256[:16]}...`)",
            f"- **Policy:** v{self.policy.version} (`{self.policy.source}`, sha256 `{self.policy.sha256[:16]}...`)",
            f"- **LLM agents:** {'on, model ' + (self.request.llm_model or self.cfg.default_model) if self.request.use_llm else 'off'}",
            "",
            "## Decision chain",
            "",
            "| # | Step | Status | Summary |",
            "|---|---|---|---|",
        ]
        for s in steps:
            detail = {k: v for k, v in s["detail"].items() if k not in ("artifact",)}
            lines.append(f"| {s['seq']} | {s['step']} | {s['status']} | `{str(detail)[:220]}` |")
        if decision:
            lines += ["", "## Measurements (server execution time, ms)", "",
                      "| Query | Kind | Weight | Before median [p25-p75] | After median [p25-p75] | Ratio | 95% CI | p (faster) | p (slower) |",
                      "|---|---|---|---|---|---|---|---|---|"]
            for qid, q in decision["queries"].items():
                b, a = q["baseline"], q["after"]
                lines.append(
                    f"| {qid} | {q['role']} | {q['weight']} | {b['median']:.3f} [{b['p25']:.3f}-{b['p75']:.3f}] | "
                    f"{a['median']:.3f} [{a['p25']:.3f}-{a['p75']:.3f}] | {q['median_ratio']:.3f} | "
                    f"[{q['ratio_ci95'][0]:.3f}, {q['ratio_ci95'][1]:.3f}] | {q['p_faster']:.2e} | {q['p_slower']:.2e} |"
                )
            w = decision["weighted"]
            lines += ["", f"Weighted workload: {w['before']:.3f} -> {w['after']:.3f} (ratio {w['ratio']:.3f})", "",
                      "## Acceptance criteria", "", "| Criterion | Status | Detail |", "|---|---|---|"]
            for c in decision["criteria"]:
                lines.append(f"| {c['name']} | {c['status']} | {c['detail']} |")
            lines += ["", f"**Outcome: {decision['outcome']}**"]
        lines += ["", "## Evidence files", "", "| File | Kind | Bytes | sha256 |", "|---|---|---|---|"]
        for a in artifacts:
            lines.append(f"| {a['name']} | {a['kind']} | {a['bytes']} | `{a['sha256']}` |")
        self._artifact("report.md", "report", "\n".join(lines) + "\n")
        self._artifact("operation.json", "record", {**self.record, "status": status, "message": message,
                                                     "steps": steps, "changes": self.journal.changes(self.op)})


def recover(cfg: AppConfig, target: TargetConfig, adapter, policy: Policy, actor: str = ACTOR_CLI) -> list[dict]:
    """
    Brings every interrupted operation on ``target`` to an explicit terminal
    state, and removes any Field Horizon index the journal does not show as
    retained. Idempotent: a second run finds nothing to do.

    The rule is conservative by construction: an index whose measurement and
    decision did not complete is reverted, never kept -- an unmeasured
    change is not an improvement.
    """
    journal = Journal(cfg)
    actions: list[dict] = []
    retained = set()
    for record in journal.recent(limit=10_000):
        if record.target != target.name:
            continue
        for change in journal.changes(record.operation_id):
            if change["state"] == "retained":
                retained.add((change["object_schema"], change["object_name"]))

    for record in journal.recent(limit=10_000):
        if record.target != target.name:
            continue
        needs = record.finished_at is None or record.status == STATUS_FAILED_NEEDS_RECOVERY
        changes = [c for c in journal.changes(record.operation_id) if c["state"] not in ("retained", "reverted")]
        if not needs and not changes:
            continue
        emitter = OperationEmitter(cfg, actor=actor, aggregate_type=AGGREGATE_DB_OPERATION, correlation_id=record.operation_id)
        undone = []
        for change in changes:
            state = adapter.index_state(change["object_schema"], change["object_name"])
            if state is not None:
                owner = (state.marker or {}).get("operation_id")
                if owner not in (record.operation_id, None):
                    actions.append({"operation_id": record.operation_id, "index": change["object_name"],
                                    "action": "left alone: index belongs to another operation", "owner": owner})
                    continue
                adapter.drop_index_concurrently(change["object_schema"], change["object_name"], lock_timeout_ms=policy.lock_timeout_ms)
            if adapter.index_state(change["object_schema"], change["object_name"]) is None:
                journal.change_state(record.operation_id, change["change_id"], "reverted")
                undone.append({"index": f"{change['object_schema']}.{change['object_name']}", "was_present": state is not None,
                               "was_valid": state.valid if state else None})
        status = STATUS_RECOVERED_REVERTED if record.finished_at is None else STATUS_FAILED_RECOVERED
        message = f"Recovered after interruption in state {record.state!r}: " + (
            f"reverted {undone}" if undone else "no change had reached the database")
        journal.finish(record.operation_id, status, message)
        emitter.emit(EVT_DB_OPERATION_RECOVERED, aggregate_id=record.operation_id, payload={"status": status, "undone": undone})
        actions.append({"operation_id": record.operation_id, "previous_state": record.state, "status": status, "undone": undone})

    for state in adapter.fieldhorizon_indexes():
        if (state.schema, state.name) in retained and state.valid:
            continue
        owner = (state.marker or {}).get("operation_id")
        if owner is None:
            # Named like ours but unmarked and in no ledger: not provably ours, so never dropped automatically.
            # (Ours-but-unmarked -- a crash between CREATE INDEX and COMMENT -- is in a ledger and handled above.)
            actions.append({"index": f"{state.schema}.{state.name}", "action": "left alone: no Field Horizon marker", "valid": state.valid})
        elif journal.get(owner) is None:
            actions.append({"index": f"{state.schema}.{state.name}", "action": "left alone: marker names an operation unknown to this journal",
                            "owner": owner})
        else:
            adapter.drop_index_concurrently(state.schema, state.name, lock_timeout_ms=policy.lock_timeout_ms)
            actions.append({"index": f"{state.schema}.{state.name}", "action": "dropped Field Horizon index with no retained decision",
                            "valid": state.valid, "owner": owner})
    return actions


def operation_detail(cfg: AppConfig, operation_id: str) -> dict | None:
    journal = Journal(cfg)
    record = journal.get(operation_id)
    if record is None:
        return None
    return {
        "operation": record.__dict__,
        "steps": journal.steps(operation_id),
        "changes": journal.changes(operation_id),
        "artifacts": journal.artifacts(operation_id),
    }
