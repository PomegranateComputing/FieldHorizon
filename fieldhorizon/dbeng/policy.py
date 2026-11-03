"""
The deterministic policy engine: the only authority over a change.

Three questions, each answered by explicit checks with a status of pass,
fail or unverified (an unverified check blocks when the policy says so, and
the shipped policy says so):

* authorize_write   -- may this operation write to this target at all?
* evaluate_candidate -- may this particular index be built here, now?
* decide            -- do the measurements justify keeping it?

No LLM output is an input to any of them.
"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .model import AVAILABLE, Candidate
from .stats import bootstrap_median_ratio_ci, mann_whitney_less, summarize
from .targets import TargetConfig

PASS = "pass"
FAIL = "fail"
UNVERIFIED = "unverified"

RETAIN = "RETAIN"
REVERT = "REVERT"

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "dbeng_policy.yaml"


@dataclass(frozen=True)
class Policy:
    version: int
    sha256: str
    source: str
    write_environments: tuple[str, ...]
    require_instance_marker: bool
    require_system_identifier: bool
    allowed_operations: tuple[str, ...]
    methods: tuple[str, ...]
    max_columns: int
    forbidden_schemas: tuple[str, ...]
    protected_tables: tuple[str, ...]
    max_indexes_per_table: int
    min_table_rows: int
    max_equality_selectivity: float
    max_index_bytes: int
    min_free_disk_multiple: float
    max_transaction_seconds: float
    block_on_unverified: bool
    lock_timeout_ms: int
    statement_timeout_ms: int
    warmup_runs: int
    baseline_rounds: int
    after_rounds: int
    min_target_improvement: float
    alpha: float
    max_noise: float
    max_query_regression: float
    max_write_regression: float
    min_weighted_improvement: float
    min_index_usage: float
    # --- policy v2 (absent from v1 files: v1 defaults keep v1 behaviour) ------------
    lease_scope: str = "index_optimization"
    max_authorization_age_seconds: float = 300.0
    recovery_automatic: bool = False
    orphan_wait_seconds: float = 60.0
    client_timing: bool = False
    max_control_drift: float | None = None
    vacuum_before_phase: bool = False

    def describe(self) -> dict:
        return asdict(self)


def policy_from_bytes(data: bytes, source: str) -> Policy:
    raw = yaml.safe_load(data) or {}
    a, c, e, b, acc = (raw.get(k, {}) for k in ("authority", "index_candidates", "execution", "benchmark", "acceptance"))
    co, rec, me = (raw.get(k) or {} for k in ("coordination", "recovery", "measurement"))
    if b.get("baseline_rounds", 0) < 2 or b["baseline_rounds"] % 2:
        raise ValueError("benchmark.baseline_rounds must be an even number >= 2 (it is split for the A/A noise check)")
    drift = me.get("max_control_drift")
    return Policy(
        version=int(raw["policy_version"]),
        sha256=hashlib.sha256(data).hexdigest(),
        source=source,
        write_environments=tuple(a["write_environments"]),
        require_instance_marker=bool(a["require_instance_marker"]),
        require_system_identifier=bool(a["require_system_identifier"]),
        allowed_operations=tuple(a["allowed_operations"]),
        methods=tuple(c["methods"]),
        max_columns=int(c["max_columns"]),
        forbidden_schemas=tuple(c["forbidden_schemas"]),
        protected_tables=tuple(c.get("protected_tables") or ()),
        max_indexes_per_table=int(c["max_indexes_per_table"]),
        min_table_rows=int(c["min_table_rows"]),
        max_equality_selectivity=float(c["max_equality_selectivity"]),
        max_index_bytes=int(c["max_index_bytes"]),
        min_free_disk_multiple=float(c["min_free_disk_multiple"]),
        max_transaction_seconds=float(c["max_transaction_seconds"]),
        block_on_unverified=bool(c["block_on_unverified"]),
        lock_timeout_ms=int(e["lock_timeout_ms"]),
        statement_timeout_ms=int(e["statement_timeout_ms"]),
        warmup_runs=int(b["warmup_runs"]),
        baseline_rounds=int(b["baseline_rounds"]),
        after_rounds=int(b["after_rounds"]),
        min_target_improvement=float(acc["min_target_improvement"]),
        alpha=float(acc["alpha"]),
        max_noise=float(acc["max_noise"]),
        max_query_regression=float(acc["max_query_regression"]),
        max_write_regression=float(acc["max_write_regression"]),
        min_weighted_improvement=float(acc["min_weighted_improvement"]),
        min_index_usage=float(acc["min_index_usage"]),
        lease_scope=str(co.get("lease_scope", "index_optimization")),
        max_authorization_age_seconds=float(co.get("max_authorization_age_seconds", 300)),
        recovery_automatic=bool(rec.get("automatic", False)),
        orphan_wait_seconds=float(rec.get("orphan_wait_seconds", 60)),
        client_timing=bool(me.get("client_timing", False)),
        max_control_drift=float(drift) if drift is not None else None,
        vacuum_before_phase=bool(me.get("vacuum_before_phase", False)),
    )


def load_policy(path: Path | None = None) -> Policy:
    path = path or DEFAULT_POLICY_PATH
    return policy_from_bytes(path.read_bytes(), str(path))


@dataclass
class Check:
    name: str
    status: str
    detail: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    subject: str
    checks: list[Check]
    block_on_unverified: bool = True

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL or (c.status == UNVERIFIED and self.block_on_unverified)]

    @property
    def eligible(self) -> bool:
        return not self.blocking

    def describe(self) -> dict:
        return {
            "subject": self.subject,
            "eligible": self.eligible,
            "blocking": [c.name for c in self.blocking],
            "checks": [asdict(c) for c in self.checks],
        }


def authorize_write(target: TargetConfig, identity: dict, policy: Policy, operation: str) -> Verdict:
    checks = [
        Check(
            "environment_permits_writes",
            PASS if target.environment in policy.write_environments else FAIL,
            f"target environment {target.environment!r}; writable environments {list(policy.write_environments)}",
        ),
        Check(
            "operation_allowed",
            PASS if operation in policy.allowed_operations else FAIL,
            f"{operation!r} in {list(policy.allowed_operations)}",
        ),
        Check(
            "operator_identity_declared",
            PASS if target.operator_user else FAIL,
            f"operator_user={target.operator_user!r}",
        ),
        Check(
            "server_is_primary",
            PASS if identity.get("in_recovery") is False else FAIL,
            f"pg_is_in_recovery()={identity.get('in_recovery')}",
        ),
    ]
    if policy.require_instance_marker:
        live, pinned = identity.get("instance_marker"), target.instance_marker
        status = UNVERIFIED if not live or not pinned else (PASS if live == pinned else FAIL)
        checks.append(Check("instance_marker_matches", status, f"live={live!r} pinned={pinned!r}"))
    if policy.require_system_identifier:
        live, pinned = identity.get("system_identifier"), target.system_identifier
        status = UNVERIFIED if not live or not pinned else (PASS if live == pinned else FAIL)
        detail = f"live={live!r} pinned={pinned!r}"
        if not live:
            detail += f" ({identity.get('system_identifier_detail') or 'pg_control_system() not readable'})"
        checks.append(Check("system_identifier_matches", status, detail))
    return Verdict(subject=f"write:{target.name}:{operation}", checks=checks, block_on_unverified=True)


def estimate_index_bytes(estimated_rows: int, avg_widths: list[int]) -> int:
    """Leaf tuples (8-byte header + 6-byte heap pointer, data rounded to 8) at the default 90% fillfactor, plus ~1% inner pages."""
    data = sum(avg_widths)
    tuple_bytes = 8 + 6 + ((data + 7) // 8) * 8
    return int(max(estimated_rows, 1) * (tuple_bytes + 4) / 0.9 * 1.01) + 8192 * 2


def equality_selectivity(stats: dict, estimated_rows: int) -> float | None:
    """Worst-case fraction of rows one equality value matches, from pg_stats; None if unknown."""
    if not stats:
        return None
    mcf = stats.get("mcf") or []
    n_distinct = stats.get("n_distinct")
    if mcf:
        return float(max(mcf))
    if n_distinct is None:
        return None
    n_distinct = float(n_distinct)
    if n_distinct > 0:
        return 1.0 / n_distinct
    if n_distinct < 0 and estimated_rows > 0:
        return 1.0 / (-n_distinct * estimated_rows)
    return None


def evaluate_candidate(
    candidate: Candidate,
    facts: dict,
    column_stats: dict[str, dict],
    running_transactions: list[dict],
    policy: Policy,
    free_disk_bytes: int | None,
) -> Verdict:
    spec = candidate.spec
    qualified = f"{spec.schema}.{spec.table}"
    ev = [f"table:{qualified}"]
    checks: list[Check] = [
        Check("method_allowed", PASS if spec.method in policy.methods else FAIL, f"{spec.method} in {list(policy.methods)}"),
        Check("schema_allowed", FAIL if spec.schema in policy.forbidden_schemas else PASS, f"schema {spec.schema!r}"),
        Check("table_not_protected", FAIL if qualified in policy.protected_tables else PASS,
              f"{qualified} {'is' if qualified in policy.protected_tables else 'is not'} in protected_tables"),
        Check("column_count", PASS if 0 < len(spec.columns) <= policy.max_columns else FAIL,
              f"{len(spec.columns)} column(s), max {policy.max_columns}"),
    ]
    if not facts.get("exists"):
        checks.append(Check("table_exists", FAIL, f"{qualified} not found", ev))
        return Verdict(subject=candidate.candidate_id, checks=checks, block_on_unverified=policy.block_on_unverified)

    checks.append(Check("table_exists", PASS, f"{qualified} is a {facts['kind']}", ev))
    checks.append(Check(
        "relation_kind_supported",
        PASS if facts["kind"] == "table" and facts.get("partitioning") == AVAILABLE else FAIL,
        f"kind={facts['kind']}, partitioning={facts.get('partitioning')}", ev,
    ))
    missing = [c for c in spec.columns if c not in facts.get("columns", [])]
    checks.append(Check("columns_exist", FAIL if missing else PASS, f"missing {missing}" if missing else f"{list(spec.columns)} exist", ev))

    covering = [
        i["index"] for i in facts.get("indexes", [])
        if i["is_valid"] and not i["has_expressions"] and not i["is_partial"]
        and list(i["columns"][: len(spec.columns)]) == list(spec.columns)
    ]
    checks.append(Check("not_already_indexed", FAIL if covering else PASS,
                        f"already served by {covering}" if covering else "no valid index leads with these columns", ev))

    rows = int(facts.get("estimated_rows") or 0)
    checks.append(Check("table_large_enough", PASS if rows >= policy.min_table_rows else FAIL,
                        f"estimated rows {rows}, minimum {policy.min_table_rows}", ev))
    checks.append(Check("index_budget", PASS if facts.get("index_count", 0) < policy.max_indexes_per_table else FAIL,
                        f"{facts.get('index_count', 0)} index(es), max {policy.max_indexes_per_table}", ev))

    leading = (candidate.equality_columns or list(spec.columns))[0]
    lead_stats = column_stats.get(leading)
    selectivity = equality_selectivity(lead_stats, rows) if lead_stats else None
    stats_ev = ev + [f"column_stats:{qualified}.{leading}"]
    if selectivity is None:
        checks.append(Check("leading_column_selective", UNVERIFIED, f"no pg_stats for {leading} (table not analyzed?)", stats_ev))
    else:
        checks.append(Check(
            "leading_column_selective",
            PASS if selectivity <= policy.max_equality_selectivity else FAIL,
            f"one {leading} value matches up to {selectivity:.4%} of rows; max {policy.max_equality_selectivity:.2%}",
            stats_ev,
        ))

    widths = [column_stats.get(c, {}).get("avg_width") for c in spec.columns]
    if any(w is None for w in widths):
        estimate = None
        checks.append(Check("size_within_budget", UNVERIFIED, "avg_width unknown for some columns", stats_ev))
    else:
        estimate = estimate_index_bytes(rows, [int(w) for w in widths if w is not None])
        checks.append(Check("size_within_budget", PASS if estimate <= policy.max_index_bytes else FAIL,
                            f"estimated {estimate} bytes, max {policy.max_index_bytes}", stats_ev))
    if estimate is None or free_disk_bytes is None:
        checks.append(Check("storage_available", UNVERIFIED,
                            "free space on the data directory is unknown" if free_disk_bytes is None else "index size unknown"))
    else:
        needed = int(estimate * policy.min_free_disk_multiple)
        checks.append(Check("storage_available", PASS if free_disk_bytes >= needed else FAIL,
                            f"free {free_disk_bytes} bytes, need {needed} ({policy.min_free_disk_multiple} x estimate)"))

    checks.append(Check(
        "dependencies_verified",
        PASS if facts.get("dependency_status") == AVAILABLE else UNVERIFIED,
        "all pg_depend rows against the table are classified"
        if facts.get("dependency_status") == AVAILABLE
        else f"unclassified dependencies: {facts.get('unverified_dependencies')}",
        ev,
    ))
    checks.append(Check("operator_privilege", PASS if facts.get("operator_can_create_index") else FAIL,
                        "operator can SET ROLE to the declared table owner" if facts.get("operator_can_create_index")
                        else f"operator cannot create indexes on {qualified} (owner {facts.get('owner')})", ev))
    longest = max((t.get("xact_seconds") or 0.0 for t in running_transactions), default=0.0)
    checks.append(Check("no_long_transactions", PASS if longest <= policy.max_transaction_seconds else FAIL,
                        f"longest open transaction {longest:.1f}s, max {policy.max_transaction_seconds}s"))
    return Verdict(subject=candidate.candidate_id, checks=checks, block_on_unverified=policy.block_on_unverified)


def free_disk_bytes(target: TargetConfig) -> int | None:
    """Only knowable for a local data directory the target declares; otherwise unknown (and the check is unverified)."""
    if not target.data_directory or not Path(target.data_directory).exists():
        return None
    return shutil.disk_usage(target.data_directory).free


@dataclass
class Decision:
    outcome: str
    criteria: list[Check]
    queries: dict
    weighted: dict

    def describe(self) -> dict:
        return {
            "outcome": self.outcome,
            "criteria": [asdict(c) for c in self.criteria],
            "queries": self.queries,
            "weighted": self.weighted,
        }


def decide(
    baseline: dict[str, list[float]],
    after: dict[str, list[float]],
    roles: dict[str, str],
    weights: dict[str, float],
    target_queries: list[str],
    index_usage: dict[str, float],
    policy: Policy,
    *,
    controls: list[str] | None = None,
    client_baseline: dict[str, list[float]] | None = None,
    client_after: dict[str, list[float]] | None = None,
) -> Decision:
    """
    RETAIN only if every criterion passes; anything else REVERTs.

    ``baseline`` samples are in round order; the first and second halves
    form an A/A comparison that bounds the measurement noise, so a change
    is never credited with an "improvement" the system cannot distinguish
    from two identical runs.
    """
    criteria: list[Check] = []
    queries: dict[str, dict] = {}
    for qid in baseline:
        before, now = baseline[qid], after.get(qid, [])
        half = len(before) // 2
        a1, a2 = summarize(before[:half]), summarize(before[half:])
        sb, sa = summarize(before), summarize(now)
        ratio = sa.median / sb.median if sb.median else float("inf")
        lo, hi = bootstrap_median_ratio_ci(before, now)
        queries[qid] = {
            "role": roles.get(qid),
            "weight": weights.get(qid, 0.0),
            "baseline": asdict(sb),
            "after": asdict(sa),
            "aa_noise": abs(a1.median / a2.median - 1.0) if a2.median else float("inf"),
            "median_ratio": ratio,
            "ratio_ci95": [lo, hi],
            "p_faster": mann_whitney_less(now, before),
            "p_slower": mann_whitney_less(before, now),
            "index_usage": index_usage.get(qid),
            "control": qid in (controls or []),
        }
        cb, ca = (client_baseline or {}).get(qid), (client_after or {}).get(qid)
        if cb and ca:
            scb, sca = summarize(cb), summarize(ca)
            # Reported beside the server time, never instead of it: it includes network and driver costs.
            queries[qid]["client"] = {"baseline": asdict(scb), "after": asdict(sca),
                                      "median_ratio": sca.median / scb.median if scb.median else float("inf")}

    noisy ={q: round(v["aa_noise"], 4) for q, v in queries.items() if q in target_queries and v["aa_noise"] > policy.max_noise}
    criteria.append(Check(
        "baseline_noise_bounded", FAIL if noisy else PASS,
        f"A/A noise above {policy.max_noise:.0%}: {noisy}" if noisy
        else f"A/A baseline halves agree within {policy.max_noise:.0%} for every target query",
    ))

    for qid in target_queries:
        q = queries.get(qid)
        if q is None:
            criteria.append(Check(f"target_improved:{qid}", FAIL, "no measurement"))
            continue
        improved = (
            q["median_ratio"] <= 1.0 - policy.min_target_improvement
            and q["p_faster"] < policy.alpha
            and q["ratio_ci95"][1] < 1.0
        )
        criteria.append(Check(
            f"target_improved:{qid}", PASS if improved else FAIL,
            f"median {q['baseline']['median']:.3f} -> {q['after']['median']:.3f} ms (ratio {q['median_ratio']:.3f}, "
            f"95% CI [{q['ratio_ci95'][0]:.3f}, {q['ratio_ci95'][1]:.3f}], p={q['p_faster']:.2e}); "
            f"required ratio <= {1 - policy.min_target_improvement:.2f}, p < {policy.alpha}, CI below 1",
        ))
        usage = q.get("index_usage")
        criteria.append(Check(
            f"index_used:{qid}", PASS if usage is not None and usage >= policy.min_index_usage else FAIL,
            f"new index in {usage if usage is not None else 0:.0%} of post-change plans; required {policy.min_index_usage:.0%}",
        ))

    for qid, q in queries.items():
        if qid in target_queries:
            continue
        tolerance = policy.max_write_regression if q["role"] == "write" else policy.max_query_regression
        regressed = q["median_ratio"] > 1.0 + tolerance and q["p_slower"] < policy.alpha
        criteria.append(Check(
            f"no_regression:{qid}", FAIL if regressed else PASS,
            f"{q['role']} query median ratio {q['median_ratio']:.3f} (p_slower={q['p_slower']:.2e}); "
            f"limit {1 + tolerance:.2f} when significant",
        ))

    total_before = sum(weights.get(q, 0.0) * v["baseline"]["median"] for q, v in queries.items())
    total_after = sum(weights.get(q, 0.0) * v["after"]["median"] for q, v in queries.items())
    weighted_ratio = total_after / total_before if total_before else float("inf")
    criteria.append(Check(
        "weighted_workload_improved", PASS if weighted_ratio <= 1.0 - policy.min_weighted_improvement else FAIL,
        f"weighted workload {total_before:.3f} -> {total_after:.3f} ms-units (ratio {weighted_ratio:.3f}); "
        f"required <= {1 - policy.min_weighted_improvement:.2f}",
    ))
    if policy.max_control_drift is not None:
        # Control queries cannot be served by the new index: if THEY moved, the environment moved,
        # and no before/after difference can be credited to the change.
        drift = policy.max_control_drift
        present = [q for q in (controls or []) if q in queries]
        if not present:
            criteria.append(Check("measurement_stable", FAIL, "the workload has no control query: drift cannot be bounded"))
        else:
            moved = {q: round(queries[q]["median_ratio"], 4) for q in present if abs(queries[q]["median_ratio"] - 1.0) > drift}
            touched = [q for q in present if (queries[q]["index_usage"] or 0.0) > 0.0]
            status = FAIL if moved or touched else PASS
            detail = (f"control queries moved beyond {drift:.0%}: {moved}" if moved
                      else f"control queries used the new index (not valid controls): {touched}" if touched
                      else f"control queries {present} within {drift:.0%} of baseline")
            criteria.append(Check("measurement_stable", status, detail))
    outcome = RETAIN if all(c.status == PASS for c in criteria) else REVERT
    return Decision(
        outcome=outcome, criteria=criteria, queries=queries,
        weighted={"before": total_before, "after": total_after, "ratio": weighted_ratio},
    )
