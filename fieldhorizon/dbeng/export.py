"""
Evidence: integrity checks, recomputation, and redacted export bundles.

Three different things, kept apart because they prove different things:

* integrity    -- each evidence file still hashes to the sha256 recorded in
                  the journal when it was written. It shows the file was not
                  altered since; both live on this machine, so it is NOT an
                  independent attestation.
* recomputation -- the decision is computed again from the raw samples, with
                  the policy and workload archived in the bundle, and compared
                  with the recorded decision. The original is never replaced:
                  both are shown side by side.
* attestation  -- none is claimed. No third party signed or timestamped
                  anything; the bundle says so in ATTESTATION.txt.

Exports are redacted by default: literal values in plan conditions and SQL
text become ``?`` and local paths are masked, so query parameters and plan
literals do not leave the machine. The lab's data classification is written
into the bundle (the bench is synthetic, generated from setseed(0.42)).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ..config import AppConfig
from .journal import Journal

INTACT = "intact"
MODIFIED = "modified"
MISSING = "missing"

_PLAN_TEXT_KEYS = {"Filter", "Index Cond", "Recheck Cond", "Join Filter", "Hash Cond", "Merge Cond", "One-Time Filter",
                   "filter", "query", "sql", "statement", "backend_query"}
_PATH_KEYS = {"passfile", "holder_journal", "data_directory", "path", "journal"}
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_NUMBER_LITERAL = re.compile(r"(?<![\w$.])-?\d+(?:\.\d+)?(?![\w.])")


def redact_literals(text: str) -> str:
    """'abc'::text -> ?::text, 154829 -> ?; identifiers (fhx_orders_1a2b, $1) are left as they are."""
    return _NUMBER_LITERAL.sub("?", _STRING_LITERAL.sub("?", text))


def redact_document(value, key: str | None = None):
    if isinstance(value, dict):
        return {k: redact_document(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_document(v, key) for v in value]
    if isinstance(value, str):
        if key in _PATH_KEYS:
            return "<local path>"
        if key in _PLAN_TEXT_KEYS:
            return redact_literals(value)
    return value


def verify_artifacts(journal: Journal, operation_id: str) -> list[dict]:
    out = []
    for a in journal.artifacts(operation_id):
        path = Path(a["path"])
        if not path.exists():
            out.append({"name": a["name"], "kind": a["kind"], "recorded_sha256": a["sha256"], "current_sha256": None, "integrity": MISSING})
            continue
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        out.append({"name": a["name"], "kind": a["kind"], "recorded_sha256": a["sha256"], "current_sha256": current,
                    "bytes": a["bytes"], "integrity": INTACT if current == a["sha256"] else MODIFIED})
    return out


def _artifact_text(journal: Journal, operation_id: str, name: str) -> str | None:
    for a in journal.artifacts(operation_id):
        if a["name"] == name and Path(a["path"]).exists():
            return Path(a["path"]).read_text(encoding="utf-8")
    return None


def recompute_decision(cfg: AppConfig, operation_id: str) -> dict:
    """The decision again, from the bundle's own raw samples, policy and workload. Never written over the original."""
    from .policy import decide, policy_from_bytes
    from .workload import Measurement, Sample

    journal = Journal(cfg)
    record = journal.get(operation_id)
    if record is None:
        return {"available": False, "reason": "unknown operation"}
    texts = {n: _artifact_text(journal, operation_id, n) for n in ("policy.yaml", "workload.yaml", "baseline.json", "after.json", "decision.json")}
    missing = [n for n, t in texts.items() if t is None]
    if missing:
        return {"available": False, "reason": f"not recomputable: {missing} not in the evidence (operation ended before a decision, "
                                              "or predates DB-02 archiving of policy and workload)"}
    policy = policy_from_bytes(texts["policy.yaml"].encode("utf-8"), "evidence:policy.yaml")  # type: ignore[union-attr]
    if policy.sha256 != record.policy_sha256:
        return {"available": False, "reason": "archived policy does not hash to the policy recorded for this operation"}
    workload = yaml.safe_load(texts["workload.yaml"] or "") or {}
    roles = {str(q["id"]): q.get("kind", "read") for q in workload.get("queries", [])}
    weights = {str(q["id"]): float(q.get("weight", 1.0)) for q in workload.get("queries", [])}
    controls = [str(q["id"]) for q in workload.get("queries", []) if q.get("control")]

    def measurement(text: str) -> Measurement:
        doc = json.loads(text)
        m = Measurement(label=doc["label"], rounds=doc["rounds"])
        m.samples = [Sample(**s) for s in doc["samples"]]
        return m

    baseline, after = measurement(texts["baseline.json"]), measurement(texts["after.json"])  # type: ignore[arg-type]
    recorded = json.loads(texts["decision.json"])  # type: ignore[arg-type]
    candidate = recorded.get("candidate", {})
    again = decide(baseline.series(), after.series(), roles, weights, candidate.get("target_queries", []),
                   after.index_usage(candidate.get("index_name", "")), policy, controls=controls,
                   client_baseline=baseline.client_series(), client_after=after.client_series()).describe()
    recorded_criteria = {c["name"]: c["status"] for c in recorded.get("criteria", [])}
    again_criteria = {c["name"]: c["status"] for c in again["criteria"]}
    differences = [{"criterion": n, "recorded": recorded_criteria.get(n), "recomputed": again_criteria.get(n)}
                   for n in sorted(set(recorded_criteria) | set(again_criteria)) if recorded_criteria.get(n) != again_criteria.get(n)]
    return {
        "available": True,
        "recorded": {"outcome": recorded.get("outcome"), "criteria": recorded_criteria, "weighted_ratio": recorded.get("weighted", {}).get("ratio")},
        "recomputed": {"outcome": again["outcome"], "criteria": again_criteria, "weighted_ratio": again["weighted"]["ratio"]},
        "matches": again["outcome"] == recorded.get("outcome") and not differences,
        "differences": differences,
        "inputs": {"policy_sha256": policy.sha256, "policy_version": policy.version, "samples": len(baseline.samples) + len(after.samples)},
        "note": "bootstrap confidence intervals are seeded, so a recomputation reproduces them exactly",
    }


def verification(cfg: AppConfig, operation_id: str) -> dict:
    journal = Journal(cfg)
    files = verify_artifacts(journal, operation_id)
    return {
        "operation_id": operation_id,
        "checked_at": datetime.now(UTC).isoformat(),
        "integrity": {"files": files, "all_intact": bool(files) and all(f["integrity"] == INTACT for f in files),
                      "meaning": "each file matches the sha256 the journal recorded when it was written (same machine: not an attestation)"},
        "recomputation": recompute_decision(cfg, operation_id),
        "attestation": {"status": "none", "meaning": "no independent signature or timestamp exists for this evidence"},
    }


def build_bundle(cfg: AppConfig, operation_id: str, *, redact: bool = True, data_classification: str = "unspecified") -> bytes:
    journal = Journal(cfg)
    report = verification(cfg, operation_id)
    buffer = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for a, check in zip(journal.artifacts(operation_id), report["integrity"]["files"], strict=True):
            path = Path(a["path"])
            if not path.exists():
                manifest.append({**check, "exported": False})
                continue
            data = path.read_bytes()
            redacted = False
            if redact and a["name"].endswith(".json"):
                data = json.dumps(redact_document(json.loads(data.decode("utf-8"))), indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
                redacted = True
            elif redact and a["name"].endswith((".yaml", ".md")) and data_classification != "synthetic":
                manifest.append({**check, "exported": False, "reason": "free text from a non-synthetic target is not exported"})
                continue
            zf.writestr(a["name"], data)
            manifest.append({**check, "exported": True, "redacted": redacted, "exported_sha256": hashlib.sha256(data).hexdigest()})
        zf.writestr("MANIFEST.json", json.dumps({
            "operation_id": operation_id, "exported_at": datetime.now(UTC).isoformat(), "redacted": redact,
            "data_classification": data_classification, "files": manifest,
            "recomputation": report["recomputation"],
        }, indent=2, sort_keys=True))
        zf.writestr("ATTESTATION.txt", (
            "Integrity: recorded_sha256 is the hash of each evidence file when Field Horizon wrote it; current_sha256 is its hash at "
            "export. Equal values show the file was not altered since -- on this machine. A redacted file is a transformed copy: "
            "exported_sha256 is the hash of that copy, not of the original.\n"
            "Recomputation: the decision was computed again from the raw samples with the archived policy; see MANIFEST.json.\n"
            "Attestation: none. No independent party signed or timestamped this evidence.\n"
            f"Data: {data_classification}" + (" (generated by fieldhorizon.dbeng.bench from setseed(0.42); no real person or business "
                                               "is described)" if data_classification == "synthetic" else "") + "\n"
        ))
    return buffer.getvalue()
