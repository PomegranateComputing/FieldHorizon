from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .canon import load_motif_counts, record_canon_ngrams
from .config import AppConfig
from .db import connect
from .embeddings import embed_and_store_fragment
from .evaluate import CycleEvaluation, evaluate_cycle
from .events import (
    ACTOR_CLI,
    AGGREGATE_COUNCIL,
    EVT_CANON_AUDIT_COMPLETED,
    EVT_COUNCIL_COMPLETED,
    EVT_COUNCIL_FAILED,
    EVT_COUNCIL_STARTED,
    EVT_HERESY_TRIAL_COMPLETED,
    OperationEmitter,
)
from .fragments import extract_field_fragment
from .genealogy import (
    EVENT_COUNCIL_OVERTURNED,
    EVENT_COUNCIL_UPHELD,
    EVENT_PROMOTED,
    EVENT_REHABILITATED,
    EVENT_RETIRED,
    record_canon_event,
    retire_cycle,
    set_cycle_verdict,
)
from .manifests import record_operation_manifest
from .principal import PrincipalContext
from .provenance import (
    OBJECT_COUNCIL,
    OBJECT_CYCLE,
    REL_PROMOTED_BY,
    REL_REHABILITATES,
    REL_RETIRES,
    ProvenanceEdgeRepository,
)
from .retrieval import search_json
from .temporal import TemporalCanonRepository
from .weather import record_council_weather

logger = logging.getLogger(__name__)

# canon_events semantics this module writes (see db.py's canon_events
# comment for the full enum):
#   - Every examined item gets exactly one of COUNCIL_UPHELD (verdict
#     unchanged) or COUNCIL_OVERTURNED (verdict changed) -- the generic
#     "did the council change anything" record, and the source of the
#     `councils.overturned` count.
#   - A verdict *change* additionally gets the specific event describing
#     what happened: REHABILITATED (heresy -> useful/canon), RETIRED
#     (canon -> retired), and PROMOTED too if a rehabilitation's new
#     verdict is literally CANON (the normal write-time promotion event,
#     reused here since the same thing just happened via re-judgment
#     instead of first judgment).
REHABILITATED_VERDICTS = ("USEFUL_FRAGMENT", "CANON")


@dataclass
class HeresyTrialResult:
    cycle_id: int
    query: str
    old_verdict: str
    old_score: float
    new_verdict: str
    new_score: float
    rehabilitated: bool


@dataclass
class CanonAuditResult:
    cycle_id: int
    query: str
    old_score: float
    new_verdict: str
    new_score: float
    retired: bool
    reason: str | None


@dataclass
class CouncilReport:
    council_id: int
    started_at: str
    finished_at: str
    heresy_trials: list[HeresyTrialResult]
    canon_audits: list[CanonAuditResult]
    report_path: Path

    @property
    def examined(self) -> int:
        return len(self.heresy_trials) + len(self.canon_audits)

    @property
    def overturned(self) -> int:
        return sum(1 for t in self.heresy_trials if t.rehabilitated) + sum(
            1 for a in self.canon_audits if a.retired
        )


def _record_canon_event(emitter: OperationEmitter, cycle_id: int, event: str, detail: dict) -> None:
    """
    Thin wrapper so every one of this module's ~6 record_canon_event call
    sites joins the council's own event chain (same correlation_id, causation
    threaded through emitter.last_event_id) instead of each starting a
    disconnected standalone one.
    """
    domain_event = record_canon_event(
        emitter.cfg, cycle_id, event, detail,
        actor=emitter.actor, correlation_id=emitter.correlation_id, causation_id=emitter.last_event_id,
    )
    if domain_event is not None:
        emitter.adopt(domain_event.event_id)


def _record_council_edge(cfg: AppConfig, council_id: int, cycle_id: int, relation_type: str) -> int | None:
    """
    REHABILITATES/RETIRES run council -> cycle (the council acted on the
    cycle); PROMOTED_BY runs cycle -> council (the cycle was promoted by
    it) -- the same direction cycle-level PROMOTED_BY would read in
    English ("this cycle was promoted by that council"). Best-effort,
    matching record_operation_manifest's own recording at the end of
    run_council. Returns the written edge's id (for Phase D's
    canon_temporal_states.source_edge_id linkage) or None on failure.
    """
    source_type, source_id, target_type, target_id = (
        (OBJECT_CYCLE, cycle_id, OBJECT_COUNCIL, council_id)
        if relation_type == REL_PROMOTED_BY
        else (OBJECT_COUNCIL, council_id, OBJECT_CYCLE, cycle_id)
    )
    try:
        edge = ProvenanceEdgeRepository(cfg).append(source_type, source_id, target_type, target_id, relation_type)
        return edge.id
    except Exception as exc:
        logger.warning("Provenance edge recording failed for council %d, cycle %d: %s", council_id, cycle_id, exc)
        return None


def _rows_as_dicts(rows) -> list[dict]:
    # sqlite3.Row iterates its *values*, not its column names -- .keys()
    # is required here (ruff's SIM118 doesn't know this isn't a plain dict).
    return [{k: row[k] for k in row.keys()} for row in rows]  # noqa: SIM118


def select_heresy_sample(cfg: AppConfig, sample: int, min_age_days: int) -> list[dict]:
    """
    `sample` random HERESY cycles older than `min_age_days` -- candidates
    for rehabilitation against whatever canon and axioms have accumulated
    since they were first judged. Random, not most-recent: a fragment's
    age since judgment is what matters for "has the canon grown since,"
    not how recently it happened to be generated.
    """
    with connect(cfg.database) as conn:
        rows = conn.execute(
            """
            SELECT id, query, verdict, final_score, response
            FROM cycles
            WHERE verdict = 'HERESY'
              AND dry_run = 0
              AND created_at <= datetime('now', ?)
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (f"-{min_age_days} days", sample),
        ).fetchall()

    return _rows_as_dicts(rows)


def select_canon_audit_sample(cfg: AppConfig, sample: int) -> list[dict]:
    """
    The `sample` oldest active (not yet retired) CANON cycles -- the ones
    that have had the longest to be outpaced by a canon that has since
    moved on around them.
    """
    with connect(cfg.database) as conn:
        rows = conn.execute(
            """
            SELECT id, query, verdict, final_score, response
            FROM cycles
            WHERE verdict = 'CANON'
              AND dry_run = 0
              AND retired_at IS NULL
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (sample,),
        ).fetchall()

    return _rows_as_dicts(rows)


def _re_evaluate(cfg: AppConfig, row: dict) -> CycleEvaluation:
    """
    Re-evaluates the fragment's *existing* response text against the
    current evaluator: fresh axiom retrieval (generated-axiom strata that
    didn't exist at the fragment's original judgment may exist now),
    current motif statistics, and embedding-based enforcement. Nothing is
    regenerated -- the prose is exactly what it always was; only the
    judgment against it can change.
    """
    json_rows = _rows_as_dicts(search_json(cfg, row["query"]))
    motif_counts = load_motif_counts(cfg)
    return evaluate_cycle(row["response"], json_rows, motif_counts=motif_counts, cfg=cfg)


def _try_heresy(cfg: AppConfig, row: dict, emitter: OperationEmitter, council_id: int) -> HeresyTrialResult:
    new_evaluation = _re_evaluate(cfg, row)
    rehabilitated = new_evaluation.verdict in REHABILITATED_VERDICTS

    set_cycle_verdict(cfg, row["id"], new_evaluation.verdict, new_evaluation.final_score)

    detail = {
        "old_verdict": row["verdict"],
        "old_score": row["final_score"],
        "new_verdict": new_evaluation.verdict,
        "new_score": new_evaluation.final_score,
    }

    if rehabilitated:
        _record_canon_event(emitter, row["id"], EVENT_COUNCIL_OVERTURNED, detail)
        _record_canon_event(emitter, row["id"], EVENT_REHABILITATED, detail)
        rehab_edge_id = _record_council_edge(cfg, council_id, row["id"], REL_REHABILITATES)

        if new_evaluation.verdict == "CANON":
            fragment = extract_field_fragment(row["response"])
            if fragment:
                embed_and_store_fragment(cfg, row["id"], fragment)
                record_canon_ngrams(cfg, row["id"], fragment)
            _record_canon_event(emitter, row["id"], EVENT_PROMOTED, detail)
            _record_council_edge(cfg, council_id, row["id"], REL_PROMOTED_BY)

        try:
            TemporalCanonRepository(cfg).append(
                row["id"], new_evaluation.verdict, new_evaluation.final_score,
                source_event_id=emitter.last_event_id, source_edge_id=rehab_edge_id,
            )
        except Exception as exc:
            logger.warning("Temporal canon state recording failed for cycle %d: %s", row["id"], exc)
    else:
        _record_canon_event(emitter, row["id"], EVENT_COUNCIL_UPHELD, detail)

    logger.info(
        "Council: cycle %d %s (%.3f) -> %s (%.3f) [%s]",
        row["id"], row["verdict"], row["final_score"],
        new_evaluation.verdict, new_evaluation.final_score,
        "rehabilitated" if rehabilitated else "upheld as heresy",
    )

    emitter.emit(
        EVT_HERESY_TRIAL_COMPLETED,
        aggregate_id=row["id"],
        payload={"rehabilitated": rehabilitated, "new_verdict": new_evaluation.verdict},
    )

    return HeresyTrialResult(
        cycle_id=row["id"],
        query=row["query"],
        old_verdict=row["verdict"],
        old_score=row["final_score"],
        new_verdict=new_evaluation.verdict,
        new_score=new_evaluation.final_score,
        rehabilitated=rehabilitated,
    )


def _audit_canon_cycle(cfg: AppConfig, row: dict, emitter: OperationEmitter, council_id: int) -> CanonAuditResult:
    new_evaluation = _re_evaluate(cfg, row)
    retired = new_evaluation.verdict != "CANON"

    reason: str | None = None
    detail = {
        "old_score": row["final_score"],
        "new_verdict": new_evaluation.verdict,
        "new_score": new_evaluation.final_score,
    }

    if retired:
        reason = (
            f"re-evaluation scored {new_evaluation.final_score:.3f} "
            f"({new_evaluation.verdict}) -- no longer clears canon"
        )
        retire_cycle(cfg, row["id"], reason)
        _record_canon_event(emitter, row["id"], EVENT_COUNCIL_OVERTURNED, {**detail, "reason": reason})
        _record_canon_event(emitter, row["id"], EVENT_RETIRED, {**detail, "reason": reason})
        retires_edge_id = _record_council_edge(cfg, council_id, row["id"], REL_RETIRES)

        try:
            # retire_cycle never touches cycles.verdict/final_score (a
            # retired cycle keeps whatever verdict it already had forever)
            # -- the temporal row records THAT unchanged verdict, now
            # inactive, not new_evaluation's ephemeral re-scoring.
            TemporalCanonRepository(cfg).append(
                row["id"], row["verdict"], row["final_score"], active=False,
                source_event_id=emitter.last_event_id, source_edge_id=retires_edge_id,
            )
        except Exception as exc:
            logger.warning("Temporal canon state recording failed for cycle %d: %s", row["id"], exc)
    else:
        _record_canon_event(emitter, row["id"], EVENT_COUNCIL_UPHELD, detail)

    logger.info(
        "Council audit: cycle %d CANON (%.3f) -> %s (%.3f) [%s]",
        row["id"], row["final_score"], new_evaluation.verdict, new_evaluation.final_score,
        "retired" if retired else "upheld as canon",
    )

    emitter.emit(
        EVT_CANON_AUDIT_COMPLETED,
        aggregate_id=row["id"],
        payload={"retired": retired, "new_verdict": new_evaluation.verdict},
    )

    return CanonAuditResult(
        cycle_id=row["id"],
        query=row["query"],
        old_score=row["final_score"],
        new_verdict=new_evaluation.verdict,
        new_score=new_evaluation.final_score,
        retired=retired,
        reason=reason,
    )


def _write_report(
    cfg: AppConfig,
    council_id: int,
    started_at: str,
    finished_at: str,
    heresy_trials: list[HeresyTrialResult],
    canon_audits: list[CanonAuditResult],
) -> Path:
    out_dir = cfg.outputs / "councils"
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = started_at.replace(" ", "_").replace(":", "-")
    report_path = out_dir / f"council_{council_id:04d}_{stamp}.md"

    lines = [
        f"# Field Horizon Council {council_id:04d}",
        "",
        f"Convened: {started_at}",
        f"Concluded: {finished_at}",
        "",
        "The council does not create. It judges what already exists against what the",
        "canon has since become. Nothing here is generated; everything here is re-tried.",
        "",
    ]

    if heresy_trials:
        rehabilitated_count = sum(1 for t in heresy_trials if t.rehabilitated)
        lines += [
            "## Heresy Trials",
            "",
            f"Examined: {len(heresy_trials)} | Rehabilitated: {rehabilitated_count} "
            f"| Upheld: {len(heresy_trials) - rehabilitated_count}",
            "",
            "| Cycle | Query | Old | New | Verdict |",
            "|---|---|---:|---:|---|",
        ]
        for t in heresy_trials:
            verdict_label = "**REHABILITATED**" if t.rehabilitated else "upheld as heresy"
            lines.append(
                f"| `{t.cycle_id}` | {t.query[:60]} | {t.old_score:.3f} ({t.old_verdict}) "
                f"| {t.new_score:.3f} ({t.new_verdict}) | {verdict_label} |"
            )
        lines.append("")

    if canon_audits:
        retired_count = sum(1 for a in canon_audits if a.retired)
        lines += [
            "## Canon Audit",
            "",
            f"Examined: {len(canon_audits)} | Retired: {retired_count} "
            f"| Upheld: {len(canon_audits) - retired_count}",
            "",
            "| Cycle | Query | Old Score | New | Verdict |",
            "|---|---|---:|---:|---|",
        ]
        for a in canon_audits:
            verdict_label = f"**RETIRED** -- {a.reason}" if a.retired else "upheld as canon"
            lines.append(
                f"| `{a.cycle_id}` | {a.query[:60]} | {a.old_score:.3f} "
                f"| {a.new_score:.3f} ({a.new_verdict}) | {verdict_label} |"
            )
        lines.append("")

    if not heresy_trials and not canon_audits:
        lines.append("No candidates met the council's criteria this session.")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def run_council(
    cfg: AppConfig,
    sample: int = 5,
    min_age_days: int = 7,
    audit_canon: bool = False,
    audit_sample: int | None = None,
    actor: str = ACTOR_CLI,
    principal: PrincipalContext | None = None,
) -> CouncilReport:
    """
    A council examines `sample` random HERESY cycles older than
    `min_age_days` and re-evaluates each against the current canon and
    evaluator; a re-evaluation that now clears the USEFUL_FRAGMENT
    threshold is REHABILITATED. If `audit_canon`, it additionally
    re-evaluates the oldest active CANON cycles (same `sample` count,
    unless `audit_sample` overrides it); one that no longer clears the
    CANON threshold is RETIRED -- never deleted, only excluded from
    future MMR canon selection. Every council writes a `councils` row and
    a markdown report to outputs/councils/.
    """
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO councils(started_at, examined, overturned) VALUES (?, 0, 0)",
            (started_at,),
        )
        conn.commit()
        assert cur.lastrowid is not None
        council_id = int(cur.lastrowid)

    emitter = OperationEmitter(cfg, actor=actor, aggregate_type=AGGREGATE_COUNCIL, principal=principal)
    emitter.emit(EVT_COUNCIL_STARTED, aggregate_id=council_id, payload={"sample": sample, "audit_canon": audit_canon})

    try:
        heresy_trials = [
            _try_heresy(cfg, row, emitter, council_id) for row in select_heresy_sample(cfg, sample, min_age_days)
        ]

        canon_audits: list[CanonAuditResult] = []
        if audit_canon:
            audit_n = sample if audit_sample is None else audit_sample
            canon_audits = [
                _audit_canon_cycle(cfg, row, emitter, council_id) for row in select_canon_audit_sample(cfg, audit_n)
            ]
    except Exception as exc:
        emitter.emit(
            EVT_COUNCIL_FAILED,
            aggregate_id=council_id,
            payload={"error_type": type(exc).__name__, "error_message": str(exc)},
        )
        raise

    finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_path = _write_report(cfg, council_id, started_at, finished_at, heresy_trials, canon_audits)

    report = CouncilReport(
        council_id=council_id,
        started_at=started_at,
        finished_at=finished_at,
        heresy_trials=heresy_trials,
        canon_audits=canon_audits,
        report_path=report_path,
    )

    with connect(cfg.database) as conn:
        conn.execute(
            "UPDATE councils SET finished_at = ?, examined = ?, overturned = ?, notes = ? WHERE id = ?",
            (finished_at, report.examined, report.overturned, str(report_path), council_id),
        )
        conn.commit()

    record_council_weather(cfg, council_id)

    emitter.emit(
        EVT_COUNCIL_COMPLETED,
        aggregate_id=council_id,
        payload={"examined": report.examined, "overturned": report.overturned},
    )

    try:
        record_operation_manifest(
            cfg,
            run_id=emitter.correlation_id,
            operation="council",
            selected_evidence_ids=(
                [f"cycle:{t.cycle_id}" for t in heresy_trials] + [f"cycle:{a.cycle_id}" for a in canon_audits]
            ),
            output_ids=[council_id],
        )
    except Exception as exc:
        # Best-effort, matching cycle.py's own manifest recording.
        logger.warning("Manifest recording failed for council %d: %s", council_id, exc)

    return report


@dataclass(frozen=True)
class CouncilCase:
    cycle_id: int
    query: str
    verdict: str
    final_score: float


@dataclass(frozen=True)
class CouncilDetail:
    council_id: int
    rehabilitated: list[CouncilCase]
    retired: list[CouncilCase]
    promoted: list[CouncilCase]


def _cases_for(cfg: AppConfig, cycle_ids: list[int]) -> list[CouncilCase]:
    if not cycle_ids:
        return []
    with connect(cfg.database) as conn:
        placeholders = ",".join("?" for _ in cycle_ids)
        rows = conn.execute(
            f"SELECT id, query, verdict, final_score FROM cycles WHERE id IN ({placeholders})", cycle_ids
        ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    # Preserves cycle_ids' own order (the provenance edges' insertion order), not the SQL IN clause's arbitrary one.
    return [
        CouncilCase(
            cycle_id=cid, query=by_id[cid]["query"], verdict=by_id[cid]["verdict"] or "",
            final_score=float(by_id[cid]["final_score"] or 0.0),
        )
        for cid in cycle_ids
        if cid in by_id
    ]


def council_detail(cfg: AppConfig, council_id: int) -> CouncilDetail:
    """
    Which specific cycles a council rehabilitated, retired, or promoted --
    FABLE Sec.10.3's "rehabilitations and retirements", not just the
    councils table's own aggregate examined/overturned counts. Reads the
    real REHABILITATES/RETIRES/PROMOTED_BY provenance edges _record_council_edge
    wrote during the run; each case links out to that cycle's own
    /provenance page (Implementation Brief IV Phase UI-4 item 5), which
    already renders the same edge from the cycle's side alongside its full
    canon_events history -- no need to duplicate that detail here.
    """
    repo = ProvenanceEdgeRepository(cfg)
    rehab_ids = [int(e.target_id) for e in repo.edges_from(OBJECT_COUNCIL, council_id, relation_type=REL_REHABILITATES)]
    retired_ids = [int(e.target_id) for e in repo.edges_from(OBJECT_COUNCIL, council_id, relation_type=REL_RETIRES)]
    promoted_ids = [int(e.target_id) for e in repo.edges_from(OBJECT_COUNCIL, council_id, relation_type=REL_PROMOTED_BY)]

    return CouncilDetail(
        council_id=council_id,
        rehabilitated=_cases_for(cfg, rehab_ids),
        retired=_cases_for(cfg, retired_ids),
        promoted=_cases_for(cfg, promoted_ids),
    )
