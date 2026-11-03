"""
Original evaluation records, and honest recomputation against them.

A cycle's decision (cycles.final_score / cycles.verdict) has always been
persisted; its per-component breakdown was not. The reproducible bundle
filled that gap with POST /evaluation/preview, which re-scores the text with
*no* retrieved axioms -- a different evaluation context, not the original
one. For cycle 1 of the Windows qualification that turned a persisted 0.819
USEFUL_FRAGMENT into a "recomputed" 0.919 CANON: doctrinal enforcement is
vacuously 1.0 without axioms (0.76 with the cycle's six), and the stuffing
penalty vanishes with them.

This module separates three things that were conflated:

* the persisted decision -- cycles.final_score / verdict, original by
  construction, available for every cycle;
* the original components -- written here at decision time, with the
  evaluator version and the evaluation context they were computed in; for
  cycles older than schema version 7 they were never stored and are
  reported as unavailable, never reconstructed and relabeled;
* a recomputation -- evaluate_cycle re-run now, in the cycle's own recorded
  retrieval context, explicitly labeled with when, with which evaluator
  version, and how its context differs from the original one.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from .canon import MOTIF_WINDOW, load_motif_counts
from .config import AppConfig
from .db import connect
from .evaluate import EVALUATOR_VERSION, CycleEvaluation, evaluate_cycle, evaluator_parameters

ROLE_INITIAL = "initial"
ROLE_REWRITTEN = "rewritten"

STATUS_RECORDED = "recorded"
STATUS_UNAVAILABLE = "unavailable"

UNAVAILABLE_REASON = (
    "This cycle predates per-component evaluation persistence (schema version 7): only its "
    "final score and verdict were stored. The original components cannot be recovered; any "
    "breakdown shown for it is a recomputation."
)

# The json_entries fields evaluate_cycle actually reads: id + statement for
# doctrinal enforcement, category + group_name for the stuffing penalty.
_CONTEXT_FIELDS = ("id", "category", "group_name", "statement")

# Rounding applied by evaluate_cycle; deltas below this are equality.
_TOLERANCE = 0.0005


def _sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def json_context_fingerprint(json_rows: list[dict]) -> str:
    return _sha256([[str(row.get(f, "")) for f in _CONTEXT_FIELDS] for row in json_rows])


def evaluation_context(cfg: AppConfig, json_rows: list[dict], motif_counts: dict[str, int] | None) -> dict:
    """Everything outside the response text that evaluate_cycle's result depends on."""
    return {
        "input": "cycle response (FIELD_FRAGMENT extracted by the evaluator)",
        "json_refs": [str(row.get("id")) for row in json_rows],
        "json_context_sha256": json_context_fingerprint(json_rows),
        "motif_window": MOTIF_WINDOW,
        "motif_ngram_count": len(motif_counts or {}),
        "motif_counts_sha256": _sha256(sorted((motif_counts or {}).items())),
        "embedding_model": cfg.embedding_model,
        "parameters": evaluator_parameters(),
    }


def _components(evaluation: CycleEvaluation) -> dict:
    data = asdict(evaluation)
    data.pop("final_score")
    data.pop("verdict")
    return data


def record_cycle_evaluations(
    conn: sqlite3.Connection,
    cycle_id: int,
    *,
    initial: CycleEvaluation,
    rewritten: CycleEvaluation | None,
    selected: CycleEvaluation,
    context: dict,
) -> None:
    """Called inside the transaction that inserts the cycle row itself."""
    for role, evaluation in ((ROLE_INITIAL, initial), (ROLE_REWRITTEN, rewritten)):
        if evaluation is None:
            continue
        conn.execute(
            "INSERT INTO cycle_evaluations(cycle_id, role, selected, evaluator_version, final_score, verdict, "
            "components_json, context_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cycle_id,
                role,
                1 if evaluation is selected else 0,
                EVALUATOR_VERSION,
                evaluation.final_score,
                evaluation.verdict,
                json.dumps(_components(evaluation), ensure_ascii=False),
                json.dumps(context, ensure_ascii=False),
            ),
        )


@dataclass
class OriginalEvaluation:
    role: str
    selected: bool
    evaluator_version: str
    final_score: float
    verdict: str
    components: dict
    context: dict
    recorded_at: str


@dataclass
class Recomputation:
    recomputed_at: str
    evaluator_version: str
    same_evaluator_version: bool | None
    json_refs: list[str]
    missing_json_refs: list[str]
    json_context_sha256: str
    json_context_matches_original: bool | None
    motif_counts_matches_original: bool | None
    final_score: float
    verdict: str
    components: dict
    matches_persisted_decision: bool
    score_delta: float
    component_deltas: dict | None


@dataclass
class CycleEvaluationReport:
    cycle_id: int
    persisted_final_score: float | None
    persisted_verdict: str | None
    original_status: str
    original_unavailable_reason: str | None
    original: list[OriginalEvaluation] = field(default_factory=list)
    recomputation: Recomputation | None = None


def load_original_evaluations(conn: sqlite3.Connection, cycle_id: int) -> list[OriginalEvaluation]:
    rows = conn.execute(
        "SELECT role, selected, evaluator_version, final_score, verdict, components_json, context_json, recorded_at "
        "FROM cycle_evaluations WHERE cycle_id = ? ORDER BY CASE role WHEN 'initial' THEN 0 ELSE 1 END",
        (cycle_id,),
    ).fetchall()
    return [
        OriginalEvaluation(
            role=row["role"],
            selected=bool(row["selected"]),
            evaluator_version=row["evaluator_version"],
            final_score=row["final_score"],
            verdict=row["verdict"],
            components=json.loads(row["components_json"]),
            context=json.loads(row["context_json"]),
            recorded_at=row["recorded_at"],
        )
        for row in rows
    ]


def _numeric_deltas(now: dict, then: dict) -> dict:
    deltas: dict[str, object] = {}
    for key, value in now.items():
        old = then.get(key)
        if isinstance(value, bool) or isinstance(old, bool):
            if value != old:
                deltas[key] = {"original": old, "recomputed": value}
        elif isinstance(value, int | float) and isinstance(old, int | float):
            if abs(value - old) > _TOLERANCE:
                deltas[key] = round(value - old, 3)
        elif key != "notes" and value != old:
            deltas[key] = {"original": old, "recomputed": value}
    return deltas


def cycle_evaluation_report(cfg: AppConfig, cycle_id: int, *, recompute: bool = True) -> CycleEvaluationReport | None:
    with connect(cfg.database) as conn:
        cycle = conn.execute("SELECT id, response, final_score, verdict FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        if cycle is None:
            return None
        originals = load_original_evaluations(conn, cycle_id)
        refs = [r["ref"] for r in conn.execute(
            "SELECT ref FROM cycle_sources WHERE cycle_id = ? AND source_kind = 'json' ORDER BY rowid", (cycle_id,)
        )]
        found = {
            r["id"]: {k: r[k] for k in r.keys()}  # noqa: SIM118 -- sqlite3.Row iterates values
            for r in conn.execute(
                f"SELECT id, category, group_name, statement FROM json_entries WHERE id IN ({','.join('?' * len(refs))})",
                refs,
            )
        } if refs else {}

    report = CycleEvaluationReport(
        cycle_id=cycle_id,
        persisted_final_score=cycle["final_score"],
        persisted_verdict=cycle["verdict"],
        original_status=STATUS_RECORDED if originals else STATUS_UNAVAILABLE,
        original_unavailable_reason=None if originals else UNAVAILABLE_REASON,
        original=originals,
    )
    if not recompute:
        return report

    json_rows = [found[ref] for ref in refs if ref in found]
    motif_counts = load_motif_counts(cfg)
    result = evaluate_cycle(cycle["response"], json_rows, motif_counts=motif_counts, cfg=cfg)
    selected = next((o for o in originals if o.selected), None)
    context = evaluation_context(cfg, json_rows, motif_counts)
    components = _components(result)
    persisted = cycle["final_score"]

    report.recomputation = Recomputation(
        recomputed_at=datetime.now(UTC).isoformat(),
        evaluator_version=EVALUATOR_VERSION,
        same_evaluator_version=(selected.evaluator_version == EVALUATOR_VERSION) if selected else None,
        json_refs=refs,
        missing_json_refs=[ref for ref in refs if ref not in found],
        json_context_sha256=context["json_context_sha256"],
        json_context_matches_original=(
            selected.context.get("json_context_sha256") == context["json_context_sha256"] if selected else None
        ),
        motif_counts_matches_original=(
            selected.context.get("motif_counts_sha256") == context["motif_counts_sha256"] if selected else None
        ),
        final_score=result.final_score,
        verdict=result.verdict,
        components=components,
        matches_persisted_decision=(
            persisted is not None
            and abs(result.final_score - persisted) <= _TOLERANCE
            and result.verdict == cycle["verdict"]
        ),
        score_delta=round(result.final_score - persisted, 3) if persisted is not None else 0.0,
        component_deltas=_numeric_deltas(components, selected.components) if selected else None,
    )
    return report
