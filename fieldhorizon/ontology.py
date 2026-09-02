from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from .config import AppConfig
from .critic import critique_axiom_candidate
from .db import connect
from .interpreter import extract_json_object
from .llm import call_ollama
from .ontology_spec import get_ontology

logger = logging.getLogger(__name__)


@dataclass
class OntologyNode:
    id: str
    category: str
    tradition: str
    statement: str
    severity: float
    mutation_potential: float
    gloss: str = ""


@dataclass
class PressureEdge:
    source_id: str
    target_id: str
    source_category: str
    target_category: str
    pressure_type: str
    score: float
    reason: str


def row_to_node(row: sqlite3.Row) -> OntologyNode:
    return OntologyNode(
        id=row["id"],
        category=row["category"] or "",
        tradition=row["tradition"] or "",
        statement=row["statement"] or "",
        severity=float(row["severity"] or 0),
        mutation_potential=float(row["mutation_potential"] or 0),
        gloss=row["gloss"] or "",
    )


def load_nodes(cfg: AppConfig) -> list[OntologyNode]:
    with connect(cfg.database) as conn:
        rows = conn.execute(
            """
            SELECT id, category, tradition, statement, gloss, severity, mutation_potential
            FROM json_entries
            ORDER BY id ASC
            """
        ).fetchall()

    return [row_to_node(row) for row in rows]


def pressure_between(a: OntologyNode, b: OntologyNode) -> PressureEdge | None:
    reason = get_ontology().opposition_reason(a.category, b.category)

    if not reason:
        return None

    score = round(
        ((a.severity + b.severity) / 2.0)
        + ((a.mutation_potential + b.mutation_potential) / 4.0),
        3,
    )

    return PressureEdge(
        source_id=a.id,
        target_id=b.id,
        source_category=a.category,
        target_category=b.category,
        pressure_type="ontological_pressure",
        score=score,
        reason=reason,
    )


def build_pressure_edges(nodes: list[OntologyNode]) -> list[PressureEdge]:
    """
    Edges only ever exist between the (~10) category pairs ontology.yaml
    declares as opposed, so there is no reason to examine all N*(N-1)/2
    node pairs: group nodes by category first, then cross only the groups
    an opposition pair actually names.
    """
    groups: dict[str, list[OntologyNode]] = defaultdict(list)
    for node in nodes:
        groups[node.category].append(node)

    edges: list[PressureEdge] = []
    seen_pairs: set[tuple[str, str]] = set()

    for opp in get_ontology().oppositions:
        pair_key = (opp.a, opp.b) if opp.a <= opp.b else (opp.b, opp.a)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        group_a = groups.get(opp.a, [])
        group_b = groups.get(opp.b, [])

        pairs = combinations(group_a, 2) if opp.a == opp.b else (
            (a, b) for a in group_a for b in group_b
        )

        for a, b in pairs:
            edge = pressure_between(a, b)
            if edge:
                edges.append(edge)

    edges.sort(key=lambda e: e.score, reverse=True)
    return edges


def edge_to_dict(edge: PressureEdge) -> dict:
    return {
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "source_category": edge.source_category,
        "target_category": edge.target_category,
        "pressure_type": edge.pressure_type,
        "score": edge.score,
        "reason": edge.reason,
    }


def export_pressure_report(cfg: AppConfig, limit: int = 50) -> Path:
    nodes = load_nodes(cfg)
    edges = build_pressure_edges(nodes)

    out_dir = cfg.outputs / "ontology"
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "pressure_edges.json"
    md_path = out_dir / "ONTOLOGY_PRESSURE.md"

    json_path.write_text(
        json.dumps([edge_to_dict(e) for e in edges], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# Field Horizon Ontology Pressure Report",
        "",
        f"Nodes: {len(nodes)}",
        f"Pressure edges: {len(edges)}",
        "",
        "| Score | Source | Target | Reason |",
        "|---:|---|---|---|",
    ]

    for edge in edges[:limit]:
        lines.append(
            f"| {edge.score} | `{edge.source_id}` / {edge.source_category} "
            f"| `{edge.target_id}` / {edge.target_category} | {edge.reason} |"
        )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return md_path

MUTATION_SYSTEM = """You are FIELD_HORIZON_AXIOM_MUTATOR.

You are not a chat assistant.
You do not summarize or restate the two axioms given to you.

Your task: given two opposed doctrinal statements and the reason they are
in tension, produce exactly ONE new axiom that neither side could accept,
but that both positions, taken to their logical end, imply. This is a
synthesis by pressure, not a compromise or an average.

Register: severe, precise, doctrinal. No hedging, no chat, no commentary.
"""


MUTATION_PROMPT = """SOURCE_AXIOM ({source_category}):
STATEMENT: {source_statement}
GLOSS: {source_gloss}

TARGET_AXIOM ({target_category}):
STATEMENT: {target_statement}
GLOSS: {target_gloss}

OPPOSITION_REASON: {edge_reason}

Produce one new axiom that neither the source nor the target position
could accept, but that both, followed to their logical end, imply. Do not
restate either axiom. Do not merely combine their vocabulary.

Also estimate, for the new axiom alone:
- severity: 0.0-1.0, how doctrinally severe/uncompromising it is.
- mutation_potential: 0.0-1.0, how much further pressure it is likely to
  generate against other doctrines.

Return JSON ONLY, exactly this shape, no markdown, no commentary:
{{
  "statement": "...",
  "severity": 0.0,
  "mutation_potential": 0.0
}}
"""


def build_mutation_prompt(source: OntologyNode, target: OntologyNode, edge: PressureEdge) -> str:
    return (
        MUTATION_SYSTEM
        + "\n\n"
        + MUTATION_PROMPT.format(
            source_category=source.category,
            source_statement=source.statement,
            source_gloss=source.gloss or "[none]",
            target_category=target.category,
            target_statement=target.statement,
            target_gloss=target.gloss or "[none]",
            edge_reason=edge.reason,
        )
    ).strip()


def mutate_axiom(
    cfg: AppConfig,
    edge: PressureEdge,
    source: OntologyNode,
    target: OntologyNode,
    model: str | None = None,
) -> dict | None:
    """
    LLM-in-the-loop axiom mutation (review's real-upgrade tier §4),
    replacing the seven fixed templates `candidate_statement` used to map
    every opposition reason to. The LLM sees the two nodes' *actual*
    statements and glosses, not just their category names, so the output
    varies with the corpus instead of collapsing to one of seven sentences
    forever. Runs at temperature 0 (review's real-upgrade tier §5): this
    call must return parseable JSON, not high-entropy prose.
    """
    prompt = build_mutation_prompt(source, target, edge)

    try:
        raw = call_ollama(cfg, prompt, model=model, options={"temperature": 0})
        data = extract_json_object(raw)
    except Exception as exc:
        logger.warning(
            "Axiom mutation failed for edge %s -> %s: %s", edge.source_id, edge.target_id, exc
        )
        return None

    statement = str(data.get("statement", "")).strip()
    if not statement:
        return None

    def _clamped(value: object, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    return {
        "statement": statement,
        "severity": _clamped(data.get("severity"), 0.5),
        "mutation_potential": _clamped(data.get("mutation_potential"), 0.5),
        "source_edges": [
            {
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "source_category": edge.source_category,
                "target_category": edge.target_category,
                "pressure_score": edge.score,
                "reason": edge.reason,
            }
        ],
    }


def normalize_statement(statement: str) -> str:
    return " ".join(statement.lower().strip().split())


def dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen: set[str] = set()
    deduped: list[dict] = []

    for candidate in candidates:
        key = normalize_statement(candidate.get("statement", ""))
        if not key:
            continue
        if key in seen:
            continue

        seen.add(key)
        deduped.append(candidate)

    return deduped


def _source_cycle_ids_for_axiom(cfg: AppConfig, axiom_id: str) -> list[int]:
    """
    If this axiom's own raw_json carries a `source_cycle_ids` list --
    set by some prior distillation-from-canon step, if one ever populates
    it -- surface it so a newly-generated axiom can record which canon
    cycles its own sources trace to (Civilization Engine genealogy phase).
    Today this is usually [] -- nothing yet tags json_entries rows with
    cycle provenance -- but the lookup is correct and wired for when
    something does, without this codebase inventing that pipeline itself.
    """
    with connect(cfg.database) as conn:
        row = conn.execute("SELECT raw_json FROM json_entries WHERE id = ?", (axiom_id,)).fetchone()

    if row is None:
        return []

    try:
        raw = json.loads(row["raw_json"]) or {}
    except (json.JSONDecodeError, TypeError):
        return []

    ids = raw.get("source_cycle_ids")
    if not isinstance(ids, list):
        return []

    return [int(i) for i in ids if isinstance(i, int | float) or (isinstance(i, str) and i.lstrip("-").isdigit())]


def _edges_above_threshold(cfg: AppConfig, limit: int, min_score: float) -> tuple[list[PressureEdge], dict[str, OntologyNode]]:
    nodes = load_nodes(cfg)
    nodes_by_id = {n.id: n for n in nodes}
    edges = [edge for edge in build_pressure_edges(nodes) if edge.score >= min_score][:limit]
    return edges, nodes_by_id


def export_axiom_candidates(
    cfg: AppConfig,
    limit: int = 20,
    min_score: float = 1.0,
    model: str | None = None,
) -> Path:
    """
    Un-gated preview of what the mutator would produce for each pressure
    edge above `min_score` -- no critic pass, so this is cheap to inspect
    before committing to `generate-axioms`, which is the same generation
    step followed by promotion gating (see export_generated_axioms).
    """
    edges, nodes_by_id = _edges_above_threshold(cfg, limit, min_score)

    candidates = []
    for edge in edges:
        source = nodes_by_id.get(edge.source_id)
        target = nodes_by_id.get(edge.target_id)
        if source is None or target is None:
            continue

        candidate = mutate_axiom(cfg, edge, source, target, model=model)
        if candidate is not None:
            candidates.append(candidate)

    candidates = dedupe_candidates(candidates)

    out_dir = cfg.outputs / "ontology"
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "axiom_candidates.json"
    md_path = out_dir / "AXIOM_CANDIDATES.md"

    json_path.write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# Field Horizon Axiom Candidates",
        "",
        f"Candidates: {len(candidates)}",
        f"Minimum pressure score: {min_score}",
        "",
        "| Score | Source | Target | Statement |",
        "|---:|---|---|---|",
    ]

    for candidate in candidates:
        edge_info = candidate["source_edges"][0]
        lines.append(
            f"| {edge_info['pressure_score']} "
            f"| `{edge_info['source_id']}` / {edge_info['source_category']} "
            f"| `{edge_info['target_id']}` / {edge_info['target_category']} "
            f"| {candidate['statement']} |"
        )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return json_path


_STRATUM_PATTERN = re.compile(r"^generated_axioms_(\d{4,})\.json$")


def next_stratum_number(cfg: AppConfig) -> int:
    """
    Strata accumulate rather than overwrite (review's real-upgrade tier
    §4): each call to export_generated_axioms writes a new
    generated_axioms_NNNN.json instead of clobbering the last one, so
    `ingest_json_corpus`'s existing `*.json` glob picks up every stratum
    as canon accumulates geological layers of self-generated doctrine.
    """
    existing = [
        int(m.group(1))
        for path in cfg.json_corpus.glob("generated_axioms_*.json")
        if (m := _STRATUM_PATTERN.match(path.name))
    ]
    return max(existing, default=0) + 1


def export_generated_axioms(
    cfg: AppConfig,
    limit: int = 20,
    min_score: float = 1.0,
    model: str | None = None,
    critic_model: str | None = None,
) -> Path:
    """
    For each pressure edge above `min_score`: mutate an axiom candidate via
    the LLM (mutate_axiom), then route it through the critic for a
    PROMOTE/REJECT verdict (critique_axiom_candidate) -- only survivors are
    written. Provenance (source edge ids, stratum number) is recorded on
    every promoted axiom so `field-horizon lineage` can walk back to it.
    """
    edges, nodes_by_id = _edges_above_threshold(cfg, limit, min_score)
    stratum = next_stratum_number(cfg)

    promoted: list[dict] = []
    rejected_count = 0

    for edge in edges:
        source = nodes_by_id.get(edge.source_id)
        target = nodes_by_id.get(edge.target_id)
        if source is None or target is None:
            continue

        candidate = mutate_axiom(cfg, edge, source, target, model=model)
        if candidate is None:
            continue

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
            rejected_count += 1
            logger.info(
                "Axiom candidate rejected by critic (%s): %s",
                verdict["reason"],
                candidate["statement"][:80],
            )
            continue

        promoted.append({**candidate, "critic_reason": verdict["reason"]})

    promoted = dedupe_candidates(promoted)

    generated = []
    for index, candidate in enumerate(promoted, start=1):
        source_edge = candidate["source_edges"][0]

        source_cycle_ids = sorted(
            set(
                _source_cycle_ids_for_axiom(cfg, source_edge["source_id"])
                + _source_cycle_ids_for_axiom(cfg, source_edge["target_id"])
            )
        )

        generated.append(
            {
                "id": f"generated_axiom_{stratum:04d}_{index:04d}",
                "group_name": "generated_axioms",
                "category": "recursive_axiom",
                "tradition": "field_horizon_internal",
                "statement": candidate["statement"],
                "gloss": (
                    f"Generated from ontological pressure: {source_edge['reason']}. "
                    f"Critic: {candidate['critic_reason']}"
                ),
                "targets": [
                    source_edge["source_category"],
                    source_edge["target_category"],
                    "recursive_mutation",
                ],
                "tone": "severe",
                "severity": candidate["severity"],
                "mutation_potential": candidate["mutation_potential"],
                "doctrinal_axes": [
                    source_edge["source_category"],
                    source_edge["target_category"],
                    "recursive_mutation",
                ],
                "tags": [
                    "generated",
                    "ontology_pressure",
                    source_edge["source_category"],
                    source_edge["target_category"],
                    "promoted",
                ],
                "provenance": {
                    "source_edges": candidate["source_edges"],
                    "stratum": stratum,
                    "source_cycle_ids": source_cycle_ids,
                },
            }
        )

    out_path = cfg.json_corpus / f"generated_axioms_{stratum:04d}.json"
    out_path.write_text(
        json.dumps(generated, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    out_dir = cfg.outputs / "ontology"
    out_dir.mkdir(parents=True, exist_ok=True)

    md_path = out_dir / f"GENERATED_AXIOMS_{stratum:04d}.md"
    lines = [
        f"# Field Horizon Generated Axioms -- Stratum {stratum:04d}",
        "",
        f"Promoted: {len(generated)}",
        f"Rejected by critic: {rejected_count}",
        f"Minimum pressure score: {min_score}",
        "",
        "| ID | Severity | Mutation | Statement |",
        "|---|---:|---:|---|",
    ]

    for axiom in generated:
        lines.append(
            f"| `{axiom['id']}` "
            f"| {axiom['severity']} "
            f"| {axiom['mutation_potential']} "
            f"| {axiom['statement']} |"
        )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return out_path