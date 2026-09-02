from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from .config import AppConfig
from .council import run_council
from .critic import critique_axiom_candidate
from .cycle import run_cycle
from .db import connect
from .embeddings import backfill_axiom_embeddings, backfill_chunk_embeddings
from .events import (
    ACTOR_DREAM,
    AGGREGATE_DREAM_RUN,
    EVT_DREAM_COMPLETED,
    EVT_DREAM_FAILED,
    EVT_DREAM_PROPOSAL_EMITTED,
    EVT_DREAM_STARTED,
    EVT_REGION_SELECTED,
    OperationEmitter,
)
from .manifests import record_operation_manifest
from .ontology import (
    OntologyNode,
    PressureEdge,
    load_nodes,
    mutate_axiom,
    next_stratum_number,
    pressure_between,
)
from .ontology_spec import get_ontology
from .retrieval import detect_domains

logger = logging.getLogger(__name__)

OPPOSITION_COUNT_THRESHOLD = 3
DOMAIN_COUNT_THRESHOLD = 2
COUNCIL_EVERY_N_ITERATIONS = 5
EMBEDDING_MAINTENANCE_LIMIT = 20

PROPOSAL_MOTIF_COUNT_THRESHOLD = 5
PROPOSAL_CONFIDENCE_THRESHOLD = 0.5


@dataclass(frozen=True)
class DreamRegion:
    kind: str  # "opposition" | "domain" | "source"
    label: str
    query: str
    detail: dict = field(default_factory=dict)


def _opposition_regions(cfg: AppConfig) -> list[tuple[DreamRegion, float]]:
    """Opposition pairs with few json_entries axioms tagged to either side -- under-developed pressure."""
    ontology = get_ontology()
    with connect(cfg.database) as conn:
        counts = {
            row["category"]: int(row["n"])
            for row in conn.execute("SELECT category, COUNT(*) AS n FROM json_entries GROUP BY category").fetchall()
        }

    regions = []
    for opp in ontology.oppositions:
        count = counts.get(opp.a, 0) + counts.get(opp.b, 0)
        if count < OPPOSITION_COUNT_THRESHOLD:
            weight = float(OPPOSITION_COUNT_THRESHOLD - count)
            region = DreamRegion(
                kind="opposition",
                label=f"{opp.a} vs {opp.b}",
                query=f"{opp.a} versus {opp.b}: {opp.reason}",
                detail={"category_a": opp.a, "category_b": opp.b, "reason": opp.reason},
            )
            regions.append((region, weight))
    return regions


def _domain_regions(cfg: AppConfig) -> list[tuple[DreamRegion, float]]:
    """Ontology domains with few active CANON cycles addressing them."""
    ontology = get_ontology()
    with connect(cfg.database) as conn:
        canon_queries = [
            row["query"]
            for row in conn.execute(
                "SELECT query FROM cycles WHERE verdict = 'CANON' AND dry_run = 0 AND retired_at IS NULL"
            ).fetchall()
        ]

    domain_counts: dict[str, int] = defaultdict(int)
    for query in canon_queries:
        for domain in detect_domains(query):
            domain_counts[domain] += 1

    regions = []
    for domain_name in ontology.all_domain_names():
        count = domain_counts.get(domain_name, 0)
        if count < DOMAIN_COUNT_THRESHOLD:
            weight = float(DOMAIN_COUNT_THRESHOLD - count)
            spec = ontology.domain(domain_name)
            hint = next(iter(spec.hints)) if spec and spec.hints else domain_name
            region = DreamRegion(kind="domain", label=domain_name, query=hint, detail={"domain": domain_name})
            regions.append((region, weight))
    return regions


def _source_regions(cfg: AppConfig) -> list[tuple[DreamRegion, float]]:
    """Sources never cited in any active CANON cycle's lineage."""
    with connect(cfg.database) as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.title
            FROM sources s
            WHERE NOT EXISTS (
                SELECT 1 FROM cycle_sources cs
                JOIN cycles c ON c.id = cs.cycle_id
                JOIN chunks ch ON ch.canonical_ref = cs.ref
                WHERE ch.source_id = s.id
                  AND c.verdict = 'CANON' AND c.dry_run = 0 AND c.retired_at IS NULL
                  AND cs.source_kind = 'book'
            )
            """
        ).fetchall()

    return [
        (DreamRegion(kind="source", label=row["title"], query=row["title"], detail={"source_id": row["id"]}), 1.0)
        for row in rows
    ]


def coverage_regions(cfg: AppConfig) -> list[tuple[DreamRegion, float]]:
    return _opposition_regions(cfg) + _domain_regions(cfg) + _source_regions(cfg)


def choose_region(cfg: AppConfig, rng: random.Random) -> DreamRegion | None:
    """Weighted sampling over under-explored regions -- weight grows with how far below its threshold a region is."""
    candidates = coverage_regions(cfg)
    if not candidates:
        return None
    regions, weights = zip(*candidates, strict=True)
    return rng.choices(regions, weights=weights, k=1)[0]


def _matching_opposition_edge(
    cfg: AppConfig, category_a: str, category_b: str
) -> tuple[OntologyNode, OntologyNode, PressureEdge] | None:
    nodes = load_nodes(cfg)
    best: tuple[OntologyNode, OntologyNode, PressureEdge] | None = None
    for a in nodes:
        if a.category != category_a:
            continue
        for b in nodes:
            if b.category != category_b:
                continue
            edge = pressure_between(a, b)
            if edge is not None and (best is None or edge.score > best[2].score):
                best = (a, b, edge)
    return best


def run_opposition_mutation(cfg: AppConfig, region: DreamRegion, model: str | None, critic_model: str | None) -> dict:
    """
    Phase 4 mutation machinery, targeted at one specific under-developed
    opposition pair rather than the whole pressure graph: mutate_axiom
    (LLM) then critique_axiom_candidate (the same PROMOTE/REJECT gate
    export_generated_axioms uses) -- nothing here bypasses that gate. Falls
    back to a targeted cycle if no json_entries nodes exist yet for either
    category (nothing to mutate from).
    """
    match = _matching_opposition_edge(cfg, region.detail["category_a"], region.detail["category_b"])
    if match is None:
        cycle_id = run_cycle(cfg, region.query, auto_rewrite=True, actor=ACTOR_DREAM)
        return {"action": "cycle_fallback", "cycle_id": cycle_id, "reason": "no json_entries nodes for this opposition yet"}

    source, target, edge = match
    candidate = mutate_axiom(cfg, edge, source, target, model=model)
    if candidate is None:
        return {"action": "mutation", "promoted": False, "reason": "mutate_axiom returned no candidate"}

    verdict = critique_axiom_candidate(
        cfg,
        candidate_statement=candidate["statement"],
        source_statement=source.statement,
        target_statement=target.statement,
        edge_reason=edge.reason,
        source_category=source.category,
        target_category=target.category,
        model=critic_model or model,
    )

    if verdict["verdict"] != "PROMOTE":
        return {"action": "mutation", "promoted": False, "reason": verdict["reason"]}

    stratum = next_stratum_number(cfg)
    entry = {
        "id": f"generated_axiom_{stratum:04d}_0001",
        "group_name": "generated_axioms",
        "category": "recursive_axiom",
        "tradition": "field_horizon_internal",
        "statement": candidate["statement"],
        "gloss": f"Generated from ontological pressure (dream): {edge.reason}. Critic: {verdict['reason']}",
        "targets": [edge.source_category, edge.target_category, "recursive_mutation"],
        "tone": "severe",
        "severity": candidate["severity"],
        "mutation_potential": candidate["mutation_potential"],
        "doctrinal_axes": [edge.source_category, edge.target_category, "recursive_mutation"],
        "tags": ["generated", "ontology_pressure", "dream", edge.source_category, edge.target_category, "promoted"],
        "provenance": {"source_edges": candidate["source_edges"], "stratum": stratum, "source_cycle_ids": []},
    }

    # Written OUTSIDE data/ (outputs/dreams/, not cfg.json_corpus) even
    # though it's shaped exactly like a generated_axioms_NNNN.json stratum:
    # dream's own safety rail is "never modifies... anything under data/",
    # so a promoted mutation candidate is a passive artifact here -- moving
    # it into data/json_corpus/ for ingest_json_corpus to pick up is a
    # separate, explicit, human action, not something dream does itself.
    candidates_dir = cfg.outputs / "dreams" / "axiom_candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    out_path = candidates_dir / f"generated_axiom_candidate_{stratum:04d}.json"
    out_path.write_text(json.dumps([entry], ensure_ascii=False, indent=2), encoding="utf-8")

    return {"action": "mutation", "promoted": True, "statement": candidate["statement"], "path": str(out_path)}


def run_region_iteration(cfg: AppConfig, region: DreamRegion, model: str | None, critic_model: str | None) -> dict:
    """One dream iteration's action for the chosen region -- always ends up
    routed through the normal evaluator+critic (run_cycle's evaluate_cycle
    and, for auto_rewrite, critique_response; or, for an opposition region,
    mutate_axiom + critique_axiom_candidate). Nothing here bypasses a gate."""
    if region.kind == "opposition":
        return run_opposition_mutation(cfg, region, model, critic_model)

    cycle_id = run_cycle(cfg, region.query, auto_rewrite=True, critic_model=critic_model, model=model, actor=ACTOR_DREAM)
    with connect(cfg.database) as conn:
        verdict = conn.execute("SELECT verdict FROM cycles WHERE id = ?", (cycle_id,)).fetchone()["verdict"]
    return {"action": "cycle", "cycle_id": cycle_id, "verdict": verdict}


def scan_for_ontology_proposals(cfg: AppConfig) -> list[dict]:
    """
    Motifs recurring across chunks whose strongest domain-tag confidence is
    low: the knowledge graph (chunk_motifs/chunk_concepts, corpus-scale
    phase) surfacing material the ontology has no confident category for.
    Read-only scan; writing the actual proposal file is a separate step.
    """
    with connect(cfg.database) as conn:
        motif_rows = conn.execute(
            "SELECT motif, COUNT(DISTINCT chunk_id) AS n FROM chunk_motifs GROUP BY motif "
            "HAVING n >= ? ORDER BY n DESC",
            (PROPOSAL_MOTIF_COUNT_THRESHOLD,),
        ).fetchall()

    evidence: list[dict] = []
    for row in motif_rows:
        motif = row["motif"]
        with connect(cfg.database) as conn:
            chunk_ids = [
                r["chunk_id"] for r in conn.execute(
                    "SELECT chunk_id FROM chunk_motifs WHERE motif = ?", (motif,)
                ).fetchall()
            ]
            if not chunk_ids:
                continue
            placeholders = ",".join("?" for _ in chunk_ids)
            max_conf_rows = conn.execute(
                f"SELECT chunk_id, MAX(confidence) AS max_conf FROM chunk_concepts "
                f"WHERE chunk_id IN ({placeholders}) GROUP BY chunk_id",
                chunk_ids,
            ).fetchall()

        conf_by_chunk = {r["chunk_id"]: float(r["max_conf"]) for r in max_conf_rows}
        low_confidence_chunks = [cid for cid in chunk_ids if conf_by_chunk.get(cid, 0.0) < PROPOSAL_CONFIDENCE_THRESHOLD]

        if len(low_confidence_chunks) < PROPOSAL_MOTIF_COUNT_THRESHOLD:
            continue

        with connect(cfg.database) as conn:
            sample_ids = low_confidence_chunks[:3]
            placeholders = ",".join("?" for _ in sample_ids)
            sample_rows = conn.execute(
                f"SELECT id, content FROM chunks WHERE id IN ({placeholders})", sample_ids
            ).fetchall()

        evidence.append(
            {
                "motif": motif,
                "count": len(low_confidence_chunks),
                "sample_chunks": [{"id": int(r["id"]), "excerpt": r["content"][:300]} for r in sample_rows],
            }
        )

    return evidence


def _next_proposal_number(proposals_dir: Path) -> int:
    existing = [int(p.name[:4]) for p in proposals_dir.glob("[0-9][0-9][0-9][0-9]_*.yaml") if p.name[:4].isdigit()]
    return max(existing, default=0) + 1


def write_ontology_proposal(cfg: AppConfig, motif_evidence: dict) -> Path:
    """
    Writes a versioned proposal file under proposals/ontology/ -- never
    touches ontology.yaml. `field-horizon proposals apply` is the only
    path that ever merges a proposal in, and it is a separate, explicit,
    user-invoked step.
    """
    proposals_dir = cfg.root / "proposals" / "ontology"
    proposals_dir.mkdir(parents=True, exist_ok=True)

    name_slug = re.sub(r"[^a-z0-9]+", "_", motif_evidence["motif"].lower()).strip("_") or "motif"
    number = _next_proposal_number(proposals_dir)
    path = proposals_dir / f"{number:04d}_{name_slug}.yaml"

    proposal = {
        "domain": {
            "name": name_slug,
            "hints": [motif_evidence["motif"]],
            "expansions": [],
            "sources": [],
            "keywords": [motif_evidence["motif"]],
            "rewrite_directive": None,
        },
        "evidence": {
            "motif": motif_evidence["motif"],
            "count": motif_evidence["count"],
            "sample_chunks": motif_evidence["sample_chunks"],
        },
        "rationale": (
            f"The motif '{motif_evidence['motif']}' recurs across "
            f"{motif_evidence['count']} chunks whose strongest domain-tag "
            f"confidence is below {PROPOSAL_CONFIDENCE_THRESHOLD} -- the ontology "
            "has no domain that confidently accounts for this material. This "
            "proposal is inert until explicitly applied via "
            "`field-horizon proposals apply`."
        ),
    }

    path.write_text(yaml.safe_dump(proposal, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


@dataclass
class DreamRun:
    started_at: str
    finished_at: str
    iterations: int
    budget_cycles: int
    budget_minutes: float
    events: list[dict]
    proposals_written: list[str]
    ledger_path: Path
    summary_path: Path


def run_dream(
    cfg: AppConfig,
    budget_cycles: int,
    budget_minutes: float,
    seed: int | None = None,
    model: str | None = None,
    critic_model: str | None = None,
) -> DreamRun:
    """
    Foreground, budget-capped exploration loop (Civilization Engine dream
    phase). Each iteration: pick one under-explored region by weighted
    sampling, run one mutation-or-cycle step through the normal gates,
    every 5th iteration also runs a small council. Never deletes anything;
    never writes to ontology.yaml, sources.yaml, or anything under data/ --
    the only filesystem writes here are logs/dreams/, outputs/dreams/,
    proposals/ontology/, and the ordinary side effects of run_cycle/
    run_council/mutation (cycles, json_corpus strata, embeddings). This is
    a plain synchronous loop -- no thread, no daemon, no scheduler -- so
    Ctrl-C stops it exactly where the user expects.
    """
    rng = random.Random(seed)
    start_time = time.monotonic()
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    events: list[dict] = []
    iteration = 0

    def elapsed_minutes() -> float:
        return (time.monotonic() - start_time) / 60.0

    emitter = OperationEmitter(cfg, actor=ACTOR_DREAM, aggregate_type=AGGREGATE_DREAM_RUN)
    emitter.emit(EVT_DREAM_STARTED, payload={"budget_cycles": budget_cycles, "budget_minutes": budget_minutes})

    try:
        while iteration < budget_cycles and elapsed_minutes() < budget_minutes:
            iteration += 1
            region = choose_region(cfg, rng)

            if region is None:
                events.append({"iteration": iteration, "event": "no_region_available"})
                break

            emitter.emit(EVT_REGION_SELECTED, payload={"region_kind": region.kind, "region_label": region.label})

            result = run_region_iteration(cfg, region, model, critic_model)
            event = {
                "iteration": iteration,
                "event": "region_iteration",
                "region_kind": region.kind,
                "region_label": region.label,
                "result": result,
                "elapsed_minutes": round(elapsed_minutes(), 3),
            }
            events.append(event)
            logger.info("Dream iteration %d: region=%s result=%s", iteration, region.label, result.get("action"))

            if iteration % COUNCIL_EVERY_N_ITERATIONS == 0:
                report = run_council(cfg, sample=3, actor=ACTOR_DREAM)
                events.append(
                    {
                        "iteration": iteration,
                        "event": "council",
                        "council_id": report.council_id,
                        "examined": report.examined,
                        "overturned": report.overturned,
                    }
                )

        # Low-priority embedding maintenance: only if budget remains.
        if elapsed_minutes() < budget_minutes:
            chunk_count = backfill_chunk_embeddings(cfg, limit=EMBEDDING_MAINTENANCE_LIMIT, show_progress=False)
            axiom_count = backfill_axiom_embeddings(cfg, limit=EMBEDDING_MAINTENANCE_LIMIT, show_progress=False)
            if chunk_count or axiom_count:
                events.append(
                    {"event": "embedding_maintenance", "chunks_embedded": chunk_count, "axioms_embedded": axiom_count}
                )

        proposal_evidence = scan_for_ontology_proposals(cfg)
        proposals_written: list[str] = []
        for evidence in proposal_evidence:
            path = write_ontology_proposal(cfg, evidence)
            proposals_written.append(str(path))
            events.append({"event": "ontology_proposal", "motif": evidence["motif"], "path": str(path)})
            emitter.emit(EVT_DREAM_PROPOSAL_EMITTED, payload={"motif": evidence["motif"], "path": str(path)})
    except Exception as exc:
        emitter.emit(EVT_DREAM_FAILED, payload={"error_type": type(exc).__name__, "error_message": str(exc)})
        raise

    finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logs_dir = cfg.logs / "dreams"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    ledger_path = logs_dir / f"dream_{stamp}.jsonl"
    with ledger_path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    outputs_dir = cfg.outputs / "dreams"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    summary_path = outputs_dir / f"dream_{stamp}.md"
    summary_path.write_text(
        _render_summary(started_at, finished_at, iteration, budget_cycles, budget_minutes, events, proposals_written),
        encoding="utf-8",
    )

    emitter.emit(
        EVT_DREAM_COMPLETED,
        payload={"iterations": iteration, "proposals_written": len(proposals_written)},
    )

    try:
        output_ids: list[str] = []
        for event in events:
            if event.get("event") == "region_iteration":
                iteration_result = event.get("result") or {}
                cycle_id = iteration_result.get("cycle_id") if isinstance(iteration_result, dict) else None
                if cycle_id is not None:
                    output_ids.append(f"cycle:{cycle_id}")
            elif event.get("event") == "council":
                output_ids.append(f"council:{event['council_id']}")

        record_operation_manifest(
            cfg,
            run_id=emitter.correlation_id,
            operation="dream",
            random_seeds={"seed": seed} if seed is not None else None,
            output_ids=output_ids,
        )
    except Exception as exc:
        # Best-effort, matching cycle.py's own manifest recording.
        logger.warning("Manifest recording failed for dream run: %s", exc)

    return DreamRun(
        started_at=started_at,
        finished_at=finished_at,
        iterations=iteration,
        budget_cycles=budget_cycles,
        budget_minutes=budget_minutes,
        events=events,
        proposals_written=proposals_written,
        ledger_path=ledger_path,
        summary_path=summary_path,
    )


_LEDGER_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")
_SUMMARY_HEADER_RE = re.compile(
    r"Started: (?P<started_at>.+)\n"
    r"Finished: (?P<finished_at>.+)\n"
    r"Iterations: (?P<iterations>\d+) / budget (?P<budget_cycles>\d+) cycles, (?P<budget_minutes>[\d.]+) minutes"
)


@dataclass(frozen=True)
class DreamRunSummary:
    stamp: str
    started_at: str
    finished_at: str
    budget_cycles: int
    budget_minutes: float
    iterations: int
    regions_explored: int
    proposals_written: int


def _parse_summary_header(summary_path: Path) -> dict | None:
    match = _SUMMARY_HEADER_RE.search(summary_path.read_text(encoding="utf-8"))
    if match is None:
        return None
    return {
        "started_at": match["started_at"],
        "finished_at": match["finished_at"],
        "iterations": int(match["iterations"]),
        "budget_cycles": int(match["budget_cycles"]),
        "budget_minutes": float(match["budget_minutes"]),
    }


def _read_ledger_events(ledger_path: Path) -> list[dict]:
    return [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def list_dream_runs(cfg: AppConfig) -> list[DreamRunSummary]:
    """
    Every past dream run, newest first. run_dream never got its own
    database table (there is no `dream_runs` table -- see db.py); the
    ledger IS the record. Budgets/timing live only in the sibling
    outputs/dreams/*.md summary _render_summary writes (the JSONL ledger
    itself never recorded them), so both files are read per run, not just
    the JSONL. A run missing its .md summary is skipped, not guessed at.
    """
    dreams_dir = cfg.logs / "dreams"
    if not dreams_dir.exists():
        return []

    summaries = []
    for ledger_path in sorted(dreams_dir.glob("dream_*.jsonl"), reverse=True):
        stamp = ledger_path.stem.removeprefix("dream_")
        summary_path = cfg.outputs / "dreams" / f"dream_{stamp}.md"
        if not summary_path.exists():
            continue
        header = _parse_summary_header(summary_path)
        if header is None:
            continue

        events = _read_ledger_events(ledger_path)
        summaries.append(
            DreamRunSummary(
                stamp=stamp,
                regions_explored=sum(1 for e in events if e.get("event") == "region_iteration"),
                proposals_written=sum(1 for e in events if e.get("event") == "ontology_proposal"),
                **header,
            )
        )
    return summaries


def dream_run_events(cfg: AppConfig, stamp: str) -> list[dict] | None:
    """Raw events for one past dream run (region/council/proposal/embedding-maintenance detail), or None if unknown."""
    if not _LEDGER_STAMP_RE.fullmatch(stamp):
        return None
    ledger_path = cfg.logs / "dreams" / f"dream_{stamp}.jsonl"
    if not ledger_path.exists():
        return None
    return _read_ledger_events(ledger_path)


def _render_summary(
    started_at: str,
    finished_at: str,
    iterations: int,
    budget_cycles: int,
    budget_minutes: float,
    events: list[dict],
    proposals_written: list[str],
) -> str:
    lines = [
        "# Field Horizon Dream",
        "",
        f"Started: {started_at}",
        f"Finished: {finished_at}",
        f"Iterations: {iterations} / budget {budget_cycles} cycles, {budget_minutes} minutes",
        "",
        "The engine does not decide what it becomes. It notices where it is thin,",
        "and presses there, under the same gates every waking cycle answers to.",
        "",
        "## Regions Explored",
        "",
    ]

    for event in events:
        if event.get("event") == "region_iteration":
            result = event["result"]
            action = result.get("action", "?")
            detail = result.get("statement") or result.get("verdict") or result.get("reason") or ""
            lines.append(f"- iter {event['iteration']}: [{event['region_kind']}] {event['region_label']} -> {action} {detail}")
        elif event.get("event") == "council":
            lines.append(f"- iter {event['iteration']}: council #{event['council_id']} -- examined {event['examined']}, overturned {event['overturned']}")
        elif event.get("event") == "embedding_maintenance":
            lines.append(f"- embedding maintenance: {event['chunks_embedded']} chunks, {event['axioms_embedded']} axioms")

    lines.append("")
    lines.append("## Ontology Proposals")
    lines.append("")
    if proposals_written:
        for path in proposals_written:
            lines.append(f"- {path}")
    else:
        lines.append("None this run.")

    return "\n".join(lines) + "\n"
