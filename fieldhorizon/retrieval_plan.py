from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from .config import AppConfig, RetrievalWeights
from .db import connect
from .embeddings import blob_to_vector, cosine_similarity, current_embedding_version
from .events import (
    ACTOR_CLI,
    AGGREGATE_RETRIEVAL_PLAN,
    EVT_RETRIEVAL_COMPLETED,
    EVT_RETRIEVAL_PLANNED,
    OperationEmitter,
)
from .ontology_spec import get_ontology
from .provenance import OBJECT_CYCLE, build_provenance_report
from .retrieval import ScoreComponent, detect_domains, hybrid_search_chunks, terms
from .schools import school_member_cycle_ids
from .temporal import canon_valid_as_of

# The full strategy vocabulary retrieval planner v3 recognizes (Implementation
# Brief III, Phase E). Every one of these has REAL backing in this
# codebase today (see the Phase E dossier's per-strategy grounding) --
# CONTRADICTS (as opposed to OPPOSES) has no writer anywhere in this
# repository and is deliberately absent, not stubbed.
STRATEGY_LEXICAL = "LEXICAL"
STRATEGY_VECTOR = "VECTOR"
STRATEGY_DOMAIN_ROUTING = "DOMAIN_ROUTING"
STRATEGY_ENTITY = "ENTITY"
STRATEGY_MOTIF = "MOTIF"
STRATEGY_CONTRADICTION = "CONTRADICTION"
STRATEGY_CANON_GENEALOGY = "CANON_GENEALOGY"
STRATEGY_SCHOOL = "SCHOOL"
STRATEGY_TEMPORAL = "TEMPORAL"

STRATEGIES = (
    STRATEGY_LEXICAL, STRATEGY_VECTOR, STRATEGY_DOMAIN_ROUTING, STRATEGY_ENTITY, STRATEGY_MOTIF,
    STRATEGY_CONTRADICTION, STRATEGY_CANON_GENEALOGY, STRATEGY_SCHOOL, STRATEGY_TEMPORAL,
)

_BASELINE_REASON = "baseline strategy, always active"

# Oppositional cue words, alongside a declared ontology.yaml opposition
# between two query-detected domains -- either is sufficient (see
# _classify_oppositional). Deliberately small and literal, not an NLP
# classifier: this is a rule-based planner (Phase E dossier open question
# 1), so its triggers must be exhaustively enumerable and testable.
OPPOSITIONAL_KEYWORDS = {"versus", "vs", "against", "contradicts", "contradiction", "opposes", "opposition"}


@dataclass(frozen=True)
class StrategyAvailability:
    name: str
    backed: bool
    reason: str


@dataclass(frozen=True)
class RetrievalPlan:
    query: str
    strategies_selected: list[str]
    strategies_available: list[StrategyAvailability]
    classification: dict = field(default_factory=dict)


def _classify_oppositional(query: str, domains: list[str]) -> tuple[bool, list[tuple[str, str, str]]]:
    """
    Returns (is_oppositional, opposing_domain_pairs). A pair fires when
    ontology.yaml already declares an opposition between two of the
    query's own detected domains (ontology_spec.opposition_reason,
    exactly what ontology.build_pressure_edges already uses for
    axiom-vs-axiom pressure -- applied here to query-detected domains
    instead). Falls back to a literal oppositional keyword.
    """
    ontology = get_ontology()
    pairs: list[tuple[str, str, str]] = []
    for i, domain_a in enumerate(domains):
        for domain_b in domains[i + 1 :]:
            reason = ontology.opposition_reason(domain_a, domain_b)
            if reason:
                pairs.append((domain_a, domain_b, reason))

    keyword_hit = bool(set(terms(query)) & OPPOSITIONAL_KEYWORDS)
    return bool(pairs) or keyword_hit, pairs


def _matching_entities(cfg: AppConfig, query_terms: list[str]) -> list[str]:
    if not query_terms:
        return []
    with connect(cfg.database) as conn:
        placeholders = ",".join("?" for _ in query_terms)
        rows = conn.execute(
            f"SELECT DISTINCT entity FROM chunk_entities WHERE lower(entity) IN ({placeholders})",
            [t.lower() for t in query_terms],
        ).fetchall()
    return sorted({row["entity"] for row in rows})


def _matching_motifs(cfg: AppConfig, query_terms: list[str]) -> list[str]:
    if not query_terms:
        return []
    with connect(cfg.database) as conn:
        placeholders = ",".join("?" for _ in query_terms)
        rows = conn.execute(
            f"SELECT DISTINCT motif FROM chunk_motifs WHERE lower(motif) IN ({placeholders})",
            [t.lower() for t in query_terms],
        ).fetchall()
    return sorted({row["motif"] for row in rows})


def _matching_canon_cycle_ids(cfg: AppConfig, query_terms: list[str]) -> list[int]:
    """Active CANON cycles whose stored fragment textually contains a query term -- a plain LIKE scan, not a new index."""
    if not query_terms:
        return []
    with connect(cfg.database) as conn:
        clause = " OR ".join("fragment LIKE ?" for _ in query_terms)
        rows = conn.execute(
            f"SELECT id FROM cycles WHERE verdict = 'CANON' AND dry_run = 0 AND retired_at IS NULL AND ({clause}) "
            "ORDER BY id ASC",
            [f"%{t}%" for t in query_terms],
        ).fetchall()
    return [int(row["id"]) for row in rows]


def build_retrieval_plan(cfg: AppConfig, query: str, as_of: str | None = None) -> RetrievalPlan:
    """
    Classifies `query` and selects which of the backed strategies actually
    apply. LEXICAL/VECTOR/DOMAIN_ROUTING are the v2 baseline and always
    selected (matching current, unchanged behavior when nothing else
    triggers). Every other strategy activates only on a real, checkable
    signal in the query or the existing corpus -- never guessed. TEMPORAL
    never activates from the query itself (book/manifesto chunks have no
    valid-time concept, Phase E dossier §1) -- only an explicit `as_of`
    passed in by the caller activates it, exactly the same opt-in
    `plan_and_retrieve` itself honors.
    """
    query_terms = terms(query)
    domains = detect_domains(query)
    is_oppositional, opposing_pairs = _classify_oppositional(query, domains)
    entity_matches = _matching_entities(cfg, query_terms)
    motif_matches = _matching_motifs(cfg, query_terms)
    canon_matches = _matching_canon_cycle_ids(cfg, query_terms)

    selected = [STRATEGY_LEXICAL, STRATEGY_VECTOR, STRATEGY_DOMAIN_ROUTING]
    reasons = {
        STRATEGY_LEXICAL: _BASELINE_REASON,
        STRATEGY_VECTOR: _BASELINE_REASON,
        STRATEGY_DOMAIN_ROUTING: _BASELINE_REASON,
    }

    if entity_matches:
        selected.append(STRATEGY_ENTITY)
        reasons[STRATEGY_ENTITY] = f"query terms match known chunk_entities: {entity_matches}"

    if motif_matches:
        selected.append(STRATEGY_MOTIF)
        reasons[STRATEGY_MOTIF] = f"query terms match known chunk_motifs: {motif_matches}"

    if is_oppositional:
        selected.append(STRATEGY_CONTRADICTION)
        reasons[STRATEGY_CONTRADICTION] = (
            f"ontology.yaml declares opposition(s): {opposing_pairs}" if opposing_pairs else
            "oppositional keyword detected in query"
        )

    if canon_matches:
        selected.append(STRATEGY_CANON_GENEALOGY)
        reasons[STRATEGY_CANON_GENEALOGY] = f"query matches {len(canon_matches)} existing active canon fragment(s)"
        selected.append(STRATEGY_SCHOOL)
        reasons[STRATEGY_SCHOOL] = "canon match found -- surfacing sibling school membership"

    if as_of is not None:
        selected.append(STRATEGY_TEMPORAL)
        reasons[STRATEGY_TEMPORAL] = f"explicit as_of supplied: {as_of!r} -- filtering CANON_GENEALOGY candidates"

    available = [
        StrategyAvailability(
            name=name,
            backed=True,
            reason=reasons.get(
                name,
                "not selected for this query"
                if name != STRATEGY_TEMPORAL
                else "opt-in only: applies to CANON_GENEALOGY candidates when an explicit as_of is supplied",
            ),
        )
        for name in STRATEGIES
    ]

    return RetrievalPlan(
        query=query,
        strategies_selected=selected,
        strategies_available=available,
        classification={
            "detected_domains": domains,
            "oppositional": is_oppositional,
            "opposing_domain_pairs": opposing_pairs,
            "entity_matches": entity_matches,
            "motif_matches": motif_matches,
            "canon_matches": canon_matches,
        },
    )


@dataclass(frozen=True)
class ChunkCandidate:
    chunk_id: int
    canonical_ref: str
    content: str
    source_title: str
    source_type: str
    score: float
    components: dict[str, ScoreComponent]
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class CanonCandidate:
    cycle_id: int
    query: str
    fragment: str
    score: float
    components: dict[str, ScoreComponent]
    supporting_evidence_count: int = 0
    synthetic_dependency_ratio: float = 0.0
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class RetrievalPlanResult:
    plan: RetrievalPlan
    chunk_candidates: list[ChunkCandidate]
    canon_candidates: list[CanonCandidate]
    excluded_chunk_candidates: list[ChunkCandidate] = field(default_factory=list)
    excluded_canon_candidates: list[CanonCandidate] = field(default_factory=list)
    constraints_applied: list[str] = field(default_factory=list)


def _fetch_chunk_rows(cfg: AppConfig, chunk_ids: set[int]) -> dict[int, dict]:
    if not chunk_ids:
        return {}
    with connect(cfg.database) as conn:
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = conn.execute(
            f"SELECT c.id AS chunk_id, c.canonical_ref, c.content, s.title AS source_title, s.source_type "
            f"FROM chunks c JOIN sources s ON s.id = c.source_id WHERE c.id IN ({placeholders})",
            list(chunk_ids),
        ).fetchall()
    return {int(row["chunk_id"]): dict(row) for row in rows}


def _chunk_ids_for_column_values(cfg: AppConfig, table: str, column: str, values: list[str]) -> set[int]:
    """table/column are hardcoded call-site literals below, never external input."""
    if not values:
        return set()
    with connect(cfg.database) as conn:
        placeholders = ",".join("?" for _ in values)
        rows = conn.execute(
            f"SELECT DISTINCT chunk_id FROM {table} WHERE {column} IN ({placeholders})", values
        ).fetchall()
    return {int(row["chunk_id"]) for row in rows}


def _opposing_domains(opposing_pairs: list[tuple[str, str, str]]) -> list[str]:
    domains: set[str] = set()
    for domain_a, domain_b, _reason in opposing_pairs:
        domains.add(domain_a)
        domains.add(domain_b)
    return sorted(domains)


def _load_chunk_vectors(cfg: AppConfig, chunk_ids: set[int]) -> dict[int, list[float]]:
    if not chunk_ids:
        return {}
    version = current_embedding_version(cfg)
    with connect(cfg.database) as conn:
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = conn.execute(
            f"SELECT chunk_id, vector FROM chunk_embeddings WHERE chunk_id IN ({placeholders}) AND embedding_version = ?",
            (*chunk_ids, version),
        ).fetchall()
    return {int(row["chunk_id"]): blob_to_vector(row["vector"]) for row in rows}


def _annotate_diversity(candidates: list[ChunkCandidate], vectors: dict[int, list[float]]) -> list[ChunkCandidate]:
    """
    MMR-style, reusing canon._select_by_mmr's exact formula (1 minus the
    max similarity to anything already ranked above this candidate) --
    applied here over the FINAL, already-scored candidate order, as an
    informational annotation, not a re-ranking. Absent (not 0.0) for any
    candidate with no cached chunk_embeddings row -- an honest gap, not a
    guessed value.
    """
    selected_vectors: list[list[float]] = []
    annotated: list[ChunkCandidate] = []
    for candidate in candidates:
        vector = vectors.get(candidate.chunk_id)
        if vector is None:
            annotated.append(candidate)
            continue
        diversity = 1.0 if not selected_vectors else 1.0 - max(cosine_similarity(vector, v) for v in selected_vectors)
        selected_vectors.append(vector)
        components = dict(candidate.components)
        components["diversity_contribution"] = ScoreComponent(raw=diversity, weight=1.0)
        annotated.append(replace(candidate, components=components, score=candidate.score + diversity))
    return annotated


def _build_chunk_candidates(
    cfg: AppConfig, plan: RetrievalPlan, limit: int, weights: RetrievalWeights | None = None
) -> list[ChunkCandidate]:
    """
    LEXICAL/VECTOR/DOMAIN_ROUTING are exactly hybrid_search_chunks (v2),
    unchanged -- overfetched here so ENTITY/MOTIF/CONTRADICTION have room
    to reorder before the final truncation to `limit`. New candidates
    those three strategies surface (chunks v2 itself never found) are
    fetched and merged in, not just annotated onto v2's own picks -- a
    chunk found ONLY via chunk_entities/chunk_motifs/chunk_concepts still
    needs to be able to surface.
    """
    base = hybrid_search_chunks(cfg, plan.query, limit=max(limit * 3, 30), explain=True, weights=weights)
    by_id: dict[int, dict] = {
        candidate.chunk_id: {
            "canonical_ref": candidate.canonical_ref,
            "content": candidate.content,
            "source_title": candidate.source_title,
            "source_type": candidate.source_type,
            "components": dict(candidate.components),
        }
        for candidate in base
    }

    entity_ids: set[int] = set()
    motif_ids: set[int] = set()
    contradiction_ids: set[int] = set()

    if STRATEGY_ENTITY in plan.strategies_selected:
        entity_ids = _chunk_ids_for_column_values(cfg, "chunk_entities", "entity", plan.classification["entity_matches"])
    if STRATEGY_MOTIF in plan.strategies_selected:
        motif_ids = _chunk_ids_for_column_values(cfg, "chunk_motifs", "motif", plan.classification["motif_matches"])
    if STRATEGY_CONTRADICTION in plan.strategies_selected:
        opposing = _opposing_domains(plan.classification["opposing_domain_pairs"])
        contradiction_ids = _chunk_ids_for_column_values(cfg, "chunk_concepts", "domain", opposing)

    new_ids = (entity_ids | motif_ids | contradiction_ids) - set(by_id)
    for chunk_id, row in _fetch_chunk_rows(cfg, new_ids).items():
        by_id[chunk_id] = {
            "canonical_ref": row["canonical_ref"],
            "content": row["content"],
            "source_title": row["source_title"],
            "source_type": row["source_type"],
            "components": {},
        }

    for chunk_id in entity_ids & set(by_id):
        by_id[chunk_id]["components"]["entity_match"] = ScoreComponent(raw=1.0, weight=1.0)
    for chunk_id in motif_ids & set(by_id):
        by_id[chunk_id]["components"]["motif_match"] = ScoreComponent(raw=1.0, weight=1.0)
    for chunk_id in contradiction_ids & set(by_id):
        by_id[chunk_id]["components"]["contradiction_signal"] = ScoreComponent(raw=1.0, weight=1.0)

    candidates = [
        ChunkCandidate(
            chunk_id=chunk_id,
            canonical_ref=data["canonical_ref"],
            content=data["content"],
            source_title=data["source_title"],
            source_type=data["source_type"],
            score=sum(c.contribution for c in data["components"].values()),
            components=data["components"],
        )
        for chunk_id, data in by_id.items()
    ]
    candidates.sort(key=lambda c: c.score, reverse=True)
    candidates = candidates[:limit]

    vectors = _load_chunk_vectors(cfg, {c.chunk_id for c in candidates})
    return _annotate_diversity(candidates, vectors)


def _ancestor_hops(cfg: AppConfig, cycle_id: int, max_hops: int = 3) -> dict[int, int]:
    """BFS over cycles.parent_cycle_ids -- the same ancestry lineage.py already walks, capped shallow since this is a retrieval boost, not a full genealogy render."""
    hops: dict[int, int] = {}
    visited = {cycle_id}
    frontier: list[tuple[int, int]] = []

    with connect(cfg.database) as conn:
        row = conn.execute("SELECT parent_cycle_ids FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    if row is not None and row["parent_cycle_ids"]:
        try:
            frontier = [(int(pid), 1) for pid in (json.loads(row["parent_cycle_ids"]) or [])]
        except (json.JSONDecodeError, TypeError):
            frontier = []

    while frontier:
        current_id, hop = frontier.pop(0)
        if current_id in visited or hop > max_hops:
            continue
        visited.add(current_id)
        hops[current_id] = hop

        with connect(cfg.database) as conn:
            row = conn.execute("SELECT parent_cycle_ids FROM cycles WHERE id = ?", (current_id,)).fetchone()
        if row is not None and row["parent_cycle_ids"]:
            try:
                next_parents = json.loads(row["parent_cycle_ids"]) or []
            except (json.JSONDecodeError, TypeError):
                next_parents = []
            frontier.extend((int(pid), hop + 1) for pid in next_parents)

    return hops


def _active_school_ids_for_cycle(cfg: AppConfig, cycle_id: int) -> list[int]:
    with connect(cfg.database) as conn:
        latest_row = conn.execute("SELECT MAX(run_at) AS run_at FROM schools").fetchone()
        latest_run = latest_row["run_at"] if latest_row is not None else None
        if latest_run is None:
            return []
        rows = conn.execute(
            "SELECT m.school_id FROM school_members m JOIN schools s ON s.id = m.school_id "
            "WHERE m.cycle_id = ? AND s.run_at = ?",
            (cycle_id, latest_run),
        ).fetchall()
    return [int(row["school_id"]) for row in rows]


def _fetch_cycle_rows(cfg: AppConfig, cycle_ids: list[int]) -> dict[int, dict]:
    if not cycle_ids:
        return {}
    with connect(cfg.database) as conn:
        placeholders = ",".join("?" for _ in cycle_ids)
        rows = conn.execute(f"SELECT id, query, fragment FROM cycles WHERE id IN ({placeholders})", cycle_ids).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def _build_canon_candidates(cfg: AppConfig, plan: RetrievalPlan, as_of: str | None) -> list[CanonCandidate]:
    """
    CANON_GENEALOGY surfaces each matched fragment's ancestry
    (graph_proximity); SCHOOL additionally surfaces sibling cycles from
    the matched fragment's ACTIVE (most recent run_at) school generation.
    TEMPORAL is opt-in only: when `as_of` is given, candidates are
    filtered to temporal.canon_valid_as_of(as_of) -- book/manifesto chunks
    have no valid-time concept (Phase E dossier §1), so this only ever
    applies here, never to _build_chunk_candidates.
    """
    canon_matches: list[int] = plan.classification["canon_matches"]
    if STRATEGY_CANON_GENEALOGY not in plan.strategies_selected or not canon_matches:
        return []

    scores: dict[int, dict[str, ScoreComponent]] = {}
    for matched_id in canon_matches:
        scores.setdefault(matched_id, {})["direct_match"] = ScoreComponent(raw=1.0, weight=1.0)

        for ancestor_id, hop in _ancestor_hops(cfg, matched_id).items():
            proximity = 1.0 / (1 + hop)
            current = scores.setdefault(ancestor_id, {}).get("graph_proximity")
            if current is None or proximity > current.raw:
                scores[ancestor_id]["graph_proximity"] = ScoreComponent(raw=proximity, weight=1.0)

        if STRATEGY_SCHOOL in plan.strategies_selected:
            for school_id in _active_school_ids_for_cycle(cfg, matched_id):
                for sibling_id in school_member_cycle_ids(cfg, school_id):
                    if sibling_id == matched_id:
                        continue
                    scores.setdefault(sibling_id, {}).setdefault(
                        "school_membership", ScoreComponent(raw=1.0, weight=0.5)
                    )

    if STRATEGY_TEMPORAL in plan.strategies_selected and as_of is not None:
        valid_ids = set(canon_valid_as_of(cfg, as_of))
        scores = {cycle_id: components for cycle_id, components in scores.items() if cycle_id in valid_ids}

    rows = _fetch_cycle_rows(cfg, list(scores))
    candidates = []
    for cycle_id, components in scores.items():
        row = rows.get(cycle_id)
        if row is None:
            continue
        components = dict(components)
        report = build_provenance_report(cfg, OBJECT_CYCLE, cycle_id)
        components["provenance_quality"] = ScoreComponent(raw=report.completeness_score, weight=1.0)
        candidates.append(
            CanonCandidate(
                cycle_id=cycle_id,
                query=row["query"],
                fragment=row["fragment"] or "",
                score=sum(c.contribution for c in components.values()),
                components=components,
                supporting_evidence_count=report.supporting_evidence_count,
                synthetic_dependency_ratio=report.synthetic_dependency_ratio,
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


_POLICY_CONSTRAINTS = ("max_per_source", "min_direct_evidence_count", "max_synthetic_evidence_ratio")


def _apply_source_diversity(candidates: list[ChunkCandidate], max_per_source: int) -> tuple[list[ChunkCandidate], list[ChunkCandidate]]:
    """Candidates arrive pre-sorted by score -- the first `max_per_source` per source_title survive, the rest are excluded and why."""
    counts: dict[str, int] = {}
    survivors: list[ChunkCandidate] = []
    excluded: list[ChunkCandidate] = []
    for candidate in candidates:
        count = counts.get(candidate.source_title, 0)
        if count >= max_per_source:
            excluded.append(
                replace(
                    candidate,
                    exclusion_reason=f"max_per_source: source {candidate.source_title!r} already has {count} (limit {max_per_source})",
                )
            )
            continue
        counts[candidate.source_title] = count + 1
        survivors.append(candidate)
    return survivors, excluded


def _apply_canon_evidence_policy(
    candidates: list[CanonCandidate], policy_min_direct_evidence_count: int, policy_max_synthetic_evidence_ratio: float
) -> tuple[list[CanonCandidate], list[CanonCandidate]]:
    survivors: list[CanonCandidate] = []
    excluded: list[CanonCandidate] = []
    for candidate in candidates:
        if candidate.supporting_evidence_count < policy_min_direct_evidence_count:
            excluded.append(
                replace(
                    candidate,
                    exclusion_reason=(
                        f"min_direct_evidence_count: {candidate.supporting_evidence_count} SUPPORTS edge(s) "
                        f"< required {policy_min_direct_evidence_count}"
                    ),
                )
            )
            continue
        if candidate.synthetic_dependency_ratio > policy_max_synthetic_evidence_ratio:
            excluded.append(
                replace(
                    candidate,
                    exclusion_reason=(
                        f"max_synthetic_evidence_ratio: {candidate.synthetic_dependency_ratio:.2f} "
                        f"> allowed {policy_max_synthetic_evidence_ratio:.2f}"
                    ),
                )
            )
            continue
        survivors.append(candidate)
    return survivors, excluded


def plan_and_retrieve(
    cfg: AppConfig,
    query: str,
    limit: int = 10,
    as_of: str | None = None,
    actor: str = ACTOR_CLI,
    weights: RetrievalWeights | None = None,
) -> RetrievalPlanResult:
    """
    Builds a RetrievalPlan, executes exactly its selected strategies, then
    enforces cfg.retrieval_policy's three constraints -- the retrieval
    planner v3 entry point (Implementation Brief III, Phase E). All three
    constraints are always evaluated (they're global config, not
    classification-driven); excluded_chunk_candidates/excluded_canon_candidates
    carry every candidate a constraint removed, each with its own
    exclusion_reason, so nothing is silently dropped. Emits
    RetrievalPlanned then RetrievalCompleted (Phase A) under its own
    correlation chain -- EVT_RETRIEVAL_COMPLETED is the same constant
    cycle.py/multicycle.py already emit, reused, never duplicated in
    meaning, just under a separate call site.

    `weights` overrides cfg.retrieval_weights for the LEXICAL/VECTOR/
    DOMAIN_ROUTING base (Phase UI-4 item 3) -- passed straight through to
    _build_chunk_candidates' own hybrid_search_chunks call.
    """
    emitter = OperationEmitter(cfg, actor=actor, aggregate_type=AGGREGATE_RETRIEVAL_PLAN)
    plan = build_retrieval_plan(cfg, query, as_of=as_of)
    emitter.emit(
        EVT_RETRIEVAL_PLANNED,
        payload={"strategies_selected": plan.strategies_selected, "classification": plan.classification},
    )

    chunk_candidates = _build_chunk_candidates(cfg, plan, limit, weights=weights)
    canon_candidates = _build_canon_candidates(cfg, plan, as_of)

    policy = cfg.retrieval_policy
    chunk_candidates, excluded_chunks = _apply_source_diversity(chunk_candidates, policy.max_per_source)
    canon_candidates, excluded_canon = _apply_canon_evidence_policy(
        canon_candidates, policy.min_direct_evidence_count, policy.max_synthetic_evidence_ratio
    )

    emitter.emit(
        EVT_RETRIEVAL_COMPLETED,
        payload={
            "chunk_candidate_count": len(chunk_candidates),
            "canon_candidate_count": len(canon_candidates),
            "excluded_count": len(excluded_chunks) + len(excluded_canon),
        },
    )

    return RetrievalPlanResult(
        plan=plan,
        chunk_candidates=chunk_candidates,
        canon_candidates=canon_candidates,
        excluded_chunk_candidates=excluded_chunks,
        excluded_canon_candidates=excluded_canon,
        constraints_applied=list(_POLICY_CONSTRAINTS),
    )
