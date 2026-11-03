"""
Read models for the operator interface: what each screen shows, assembled from the journal,
the evidence files and (for live state) the target. Nothing here changes anything.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import AppConfig
from . import status as st
from .journal import Journal

CONTROLS_BY_STATUS = {
    st.RUNNING: ["pause", "cancel"],
    st.PAUSED: ["resume", "cancel"],
    st.CANCELLING: [],
}


def _artifact_json(journal: Journal, operation_id: str, name: str) -> Any:
    for a in journal.artifacts(operation_id):
        if a["name"] == name and Path(a["path"]).exists():
            try:
                return json.loads(Path(a["path"]).read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
    return None


def operation_summary(journal: Journal, record) -> dict:
    runtime = journal.runtime(record.operation_id) or {}
    decision = record.decision or {}
    candidate = decision.get("candidate") or (record.request or {}).get("candidate")
    return {
        "operation_id": record.operation_id,
        "kind": record.kind,
        "target": record.target,
        "status": record.status,
        "state": record.state,
        "cause": runtime.get("cause_code"),
        "message": record.message,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "live": record.finished_at is None,
        "workload": Path(str((record.request or {}).get("workload_path", ""))).stem or None,
        "candidate": candidate.get("key") if isinstance(candidate, dict) else None,
        "outcome": decision.get("outcome"),
        "weighted_ratio": (decision.get("weighted") or {}).get("ratio"),
        "recovery_mode": runtime.get("recovery_mode"),
        "recovered_by": runtime.get("recovered_by"),
        "lease_token": runtime.get("lease_token"),
        "llm": bool((record.request or {}).get("use_llm")),
    }


def policy_view(policy) -> dict:
    return {
        "version": policy.version,
        "sha256": policy.sha256,
        "source": Path(policy.source).name,
        "document": policy.describe() | {"source": Path(policy.source).name},
        "text": Path(policy.source).read_text(encoding="utf-8") if Path(policy.source).exists() else None,
    }


def target_card(service, target, *, live: bool = True) -> dict:
    card: dict[str, Any] = {
        "name": target.name, "engine": target.engine, "host": target.host, "port": target.port, "dbname": target.dbname,
        "environment": target.environment, "writable": target.writable_environment, "data_classification": target.data_classification,
        "identities": {"observer": target.observer_user, "measure": target.measure_user, "operator": target.operator_user,
                       "owner_role": target.owner_role},
        "pinned": {"instance_marker": target.instance_marker, "system_identifier": target.system_identifier},
        "fingerprint": target.fingerprint()[:16],
        "running_operation": service.running().get(target.name),
        "reconciling": target.name in service.reconciling(),
        "startup": service.startup.get(target.name),
    }
    if live:
        card["live"] = service.live(target)
    return card


def overview(service) -> dict:
    journal: Journal = service.journal
    targets = service.targets()
    policy = service.policy()
    _, recent = journal.query(kind="index_optimization", limit=12)
    _, recoveries = journal.query(kind="recovery", limit=6)
    _, last50 = journal.query(limit=50)
    live_ops = [r for r in last50 if r.finished_at is None]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": {"version": policy.version, "sha256": policy.sha256, "source": Path(policy.source).name,
                   "recovery_automatic": policy.recovery_automatic, "max_authorization_age_seconds": policy.max_authorization_age_seconds,
                   "orphan_wait_seconds": policy.orphan_wait_seconds},
        "targets": [target_card(service, t) for t in targets.values()],
        "running": [operation_summary(journal, r) for r in live_ops],
        "recent": [operation_summary(journal, r) for r in recent],
        "recoveries": [operation_summary(journal, r) for r in recoveries],
        "counts": dict(Counter(r.status for r in last50 if r.kind == "index_optimization")),
        "startup": service.startup,
    }


def operation_view(cfg: AppConfig, operation_id: str) -> dict | None:
    journal = Journal(cfg)
    record = journal.get(operation_id)
    if record is None:
        return None
    artifacts = journal.artifacts(operation_id)
    names = {a["name"] for a in artifacts}
    verdicts = (_artifact_json(journal, operation_id, "policy_verdicts.json") or {}).get("verdicts", {})
    record_doc = _artifact_json(journal, operation_id, "operation.json") or {}
    claims = _artifact_json(journal, operation_id, "claims.json")
    recovery_doc = _artifact_json(journal, operation_id, "recovery.json")
    summary = operation_summary(journal, record)
    return {
        "operation": summary,
        "request": {k: v for k, v in (record.request or {}).items() if k not in ("fault_after",)},
        "runtime": journal.runtime(operation_id),
        "controls_available": CONTROLS_BY_STATUS.get(record.status, []) if record.finished_at is None and record.kind == "index_optimization" else [],
        "timeline": journal.steps(operation_id),
        "controls": journal.controls(operation_id),
        "changes": journal.changes(operation_id),
        "decision": record.decision,
        "candidates": (_artifact_json(journal, operation_id, "candidates.json") or {}).get("candidates", []),
        "verdicts": {cid: {"key": v["candidate"]["key"], "eligible": v["eligible"], "blocking": v["blocking"], "checks": v["checks"],
                           "source": v["candidate"].get("source")} for cid, v in verdicts.items()},
        "authority": _artifact_json(journal, operation_id, "authority.json"),
        "authorization": _artifact_json(journal, operation_id, "authorization.json"),
        "change": _artifact_json(journal, operation_id, "change.json"),
        "compensation": _artifact_json(journal, operation_id, "compensation.json"),
        "final_state": _artifact_json(journal, operation_id, "final_state.json"),
        "claims": claims,
        "recovery": recovery_doc or record_doc.get("recovery"),
        "execution_failure": record_doc.get("execution_failure"),
        "lease": record_doc.get("lease"),
        "artifacts": [{k: a[k] for k in ("name", "kind", "sha256", "bytes", "created_at")} for a in artifacts],
        "report_available": "report.md" in names,
        "measurements_available": "baseline.json" in names,
    }


def measurements_view(cfg: AppConfig, operation_id: str) -> dict | None:
    journal = Journal(cfg)
    record = journal.get(operation_id)
    if record is None:
        return None
    workload = _workload_meta(journal, operation_id)
    series: dict[str, dict] = {}
    for label in ("baseline", "after", "post_compensation"):
        doc = _artifact_json(journal, operation_id, f"{label}.json")
        if not doc:
            continue
        for s in doc.get("samples", []):
            q = series.setdefault(s["query_id"], {"query_id": s["query_id"], **workload.get(s["query_id"], {}), "phases": {}})
            q["phases"].setdefault(label, []).append({"round": s["round"], "param": s["param_index"], "server_ms": s["execution_ms"],
                                                     "client_ms": s.get("client_ms"), "index_used": bool(s.get("indexes_used"))})
    decision = record.decision or {}
    for qid, q in (decision.get("queries") or {}).items():
        if qid in series:
            series[qid]["stats"] = {k: q.get(k) for k in ("baseline", "after", "median_ratio", "ratio_ci95", "p_faster", "p_slower",
                                                          "aa_noise", "index_usage", "client")}
    return {"operation_id": operation_id, "queries": list(series.values()), "weighted": decision.get("weighted"),
            "side_effects": decision.get("side_effects"), "outcome": decision.get("outcome"),
            "units": {"server_ms": "PostgreSQL execution time (EXPLAIN ANALYZE)", "client_ms": "client round trip of the plain statement",
                      "weighted": "sum of weight x median server ms: a cost, not a throughput"}}


def _workload_meta(journal: Journal, operation_id: str) -> dict:
    import yaml

    for a in journal.artifacts(operation_id):
        if a["name"] == "workload.yaml" and Path(a["path"]).exists():
            doc = yaml.safe_load(Path(a["path"]).read_text(encoding="utf-8")) or {}
            return {str(q["id"]): {"kind": q.get("kind", "read"), "weight": float(q.get("weight", 1.0)), "control": bool(q.get("control")),
                                   "sql": str(q.get("sql", "")).strip()} for q in doc.get("queries", [])}
    return {}
