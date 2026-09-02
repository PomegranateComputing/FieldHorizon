from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from .canon import load_motif_counts
from .concepts import SemanticNeighborhood, SemanticNode, semantic_neighbors, top_semantic_nodes
from .config import AppConfig, RetrievalWeights, load_config
from .council import CouncilDetail, council_detail, run_council
from .cycle import run_cycle
from .db import connect
from .dream import DreamRun, DreamRunSummary, dream_run_events, list_dream_runs, run_dream
from .evaluate import CycleEvaluation, evaluate_cycle
from .events import ACTOR_CLI, DomainEvent, EventRepository, query_events
from .fingerprint import SourceFingerprint, get_or_compute_fingerprint
from .ingest import ManifestIngestResult, ingest_books, ingest_from_manifest, ingest_json_corpus, manifest_path
from .lineage import LineageNotFoundError, build_lineage_tree
from .manifest import SourceManifestEntry, load_manifest
from .multicycle import run_multi_cycle
from .principal import PrincipalContext
from .proposals import (
    ProposalSummary,
    apply_proposal,
    list_proposals,
    reject_proposal,
    show_proposal,
)
from .provenance import (
    OBJECT_CYCLE,
    OBJECT_JSON_ENTRY,
    BackfillReport,
    ProvenanceReport,
    add_provenance_graph_section,
    backfill_provenance_edges,
    build_provenance_report_for_target,
)
from .registries import list_registry, show_registry_entry
from .replay import (
    ComparativeReplayResult,
    ConfigReplayResult,
    EvidenceReplayResult,
    HistoricalCanonReplayResult,
    ReplayResult,
    SchoolsReplayResult,
    replay_cycle,
    replay_cycle_against_historical_canon,
    replay_cycle_level2,
    replay_cycle_level3,
    replay_cycle_level5,
    replay_schools_level4,
)
from .retrieval import HybridCandidate, hybrid_search_chunks
from .retrieval_plan import RetrievalPlan, RetrievalPlanResult, build_retrieval_plan, plan_and_retrieve
from .schools import (
    ActiveSchool,
    SchoolDistance,
    SchoolMember,
    SchoolResult,
    SchoolRunEntry,
    all_schools,
    latest_schools,
    run_schools,
    school_distances_for_run,
    school_member_cycle_ids,
    school_members_detail,
)
from .temporal import TemporalBackfillReport, WhyChangedResult
from .temporal import backfill_canon_temporal_states as _backfill_canon_temporal_states
from .temporal import canon_as_of_cycle as _canon_as_of_cycle
from .temporal import canon_valid_as_of as _canon_valid_as_of
from .temporal import school_membership_as_of as _school_membership_as_of
from .temporal import weather_as_of as _weather_as_of
from .temporal import why_changed as _why_changed
from .weather import (
    DEFAULT_SURFACE_LIMIT,
    SCOPE_CANON,
    SCOPE_SURFACE,
    axis_history,
    corpus_weather,
    school_weather_profile,
)


@dataclass(frozen=True)
class CanonEntry:
    cycle_id: int
    query: str
    fragment: str
    final_score: float
    verdict: str
    created_at: str
    parent_cycle_ids: list[int]


@dataclass(frozen=True)
class CouncilSummary:
    council_id: int
    started_at: str
    finished_at: str | None
    examined: int
    overturned: int
    notes: str | None


@dataclass(frozen=True)
class WeatherSnapshot:
    canon: dict[str, float]
    surface: dict[str, float]
    surface_history: dict[str, list[float]]
    canon_history: dict[str, list[float]]


@dataclass(frozen=True)
class SchoolWeatherReading:
    school_id: int
    name: str
    readings: dict[str, float]


@dataclass(frozen=True)
class EngineStats:
    cycles_total: int
    canon_count: int
    heresy_count: int
    useful_fragment_count: int
    noise_count: int
    sources_count: int
    chunks_count: int
    councils_count: int
    schools_count: int
    dream_runs_count: int
    json_entries_count: int
    # Chunks with at least one row in chunk_concepts/chunk_entities/chunk_motifs
    # (concepts.py's real tagging pipeline) -- Phase UI-4 item 1's honest referent
    # for FABLE Sec.10.2's "semantic profiles" tile. "Semantic links" has no real
    # referent anywhere in this codebase (provenance_edges is lineage, not a
    # similarity graph) and is deliberately not represented here or in the UI.
    tagged_chunks_count: int


@dataclass(frozen=True)
class LineageResult:
    target_id: str
    text: str


@dataclass(frozen=True)
class SourceEntry:
    id: int
    title: str
    source_type: str
    manifest_id: str | None
    weight: float
    path: str
    language: str
    created_at: str
    chunk_count: int


@dataclass(frozen=True)
class ChunkEntry:
    id: int
    chunk_index: int
    canonical_ref: str
    content: str
    char_start: int | None
    char_end: int | None


@dataclass(frozen=True)
class CycleDetail:
    """A cycle's full stored row -- Phase UI-6 item 6's reproducible cycle bundle needs the query/model/prompt/response/verdict/score together, and there was no single-cycle lookup route before this (only /canon, filtered by verdict, with no cycle_id filter)."""

    cycle_id: int
    query: str
    model: str
    prompt: str
    response: str
    dry_run: bool
    verdict: str | None
    final_score: float | None
    fragment: str | None
    parent_cycle_ids: list[int]
    retired_at: str | None
    retirement_reason: str | None
    created_at: str


@dataclass(frozen=True)
class CycleSourceEntry:
    """A row of fieldhorizon/db.py's cycle_sources -- the verbatim evidence text recorded at cycle time, the same table replay.py's replay_cycle_level3 already reads for exact reproduction."""

    source_kind: str
    ref: str
    content: str


_SOURCE_QUERY = (
    "SELECT s.id, s.title, s.source_type, s.manifest_id, s.weight, s.path, s.language, s.created_at, "
    "COUNT(c.id) AS chunk_count FROM sources s LEFT JOIN chunks c ON c.source_id = s.id"
)


def _row_to_source_entry(row) -> SourceEntry:
    return SourceEntry(
        id=int(row["id"]),
        title=row["title"],
        source_type=row["source_type"],
        manifest_id=row["manifest_id"],
        weight=float(row["weight"] or 1.0),
        path=row["path"],
        language=row["language"] or "unknown",
        created_at=row["created_at"],
        chunk_count=int(row["chunk_count"]),
    )


def _row_to_canon_entry(row) -> CanonEntry:
    return CanonEntry(
        cycle_id=int(row["id"]),
        query=row["query"],
        fragment=row["fragment"] or "",
        final_score=float(row["final_score"] or 0.0),
        verdict=row["verdict"] or "",
        created_at=row["created_at"],
        parent_cycle_ids=json.loads(row["parent_cycle_ids"]) if row["parent_cycle_ids"] else [],
    )


def _canon_entries_for_ids(cfg: AppConfig, cycle_ids: list[int]) -> list[CanonEntry]:
    if not cycle_ids:
        return []
    with connect(cfg.database) as conn:
        placeholders = ",".join("?" for _ in cycle_ids)
        rows = conn.execute(
            f"SELECT id, query, fragment, final_score, verdict, created_at, parent_cycle_ids "
            f"FROM cycles WHERE id IN ({placeholders}) ORDER BY created_at DESC",
            cycle_ids,
        ).fetchall()
    return [_row_to_canon_entry(row) for row in rows]


class FieldHorizonEngine:
    """
    Single facade over every capability the platform (Phase 10) exposes to
    local consumers (CLI, server, Godot export): query/cycle, retrieve,
    canon listing, lineage, weather, schools, councils, fingerprint, stats.
    Every method here is a thin wrapper over the existing, already-gated
    modules -- no business logic lives here that doesn't already exist
    elsewhere; this class only unifies how it's reached. Behavior-
    preserving by construction: cli.py's commands for these same
    capabilities call through this facade and are verified against the
    same tests that covered the pre-facade direct calls.
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    @classmethod
    def from_config_path(cls, path: str | Path = "config.yaml") -> FieldHorizonEngine:
        return cls(load_config(path))

    def cycle(
        self,
        query: str,
        model: str | None = None,
        dry_run: bool = False,
        auto_rewrite: bool = False,
        critic_model: str | None = None,
        rewrite_model: str | None = None,
        json_domain: str | None = None,
        actor: str = ACTOR_CLI,
        principal: PrincipalContext | None = None,
    ) -> int:
        return run_cycle(
            self.cfg,
            query,
            model=model,
            dry_run=dry_run,
            auto_rewrite=auto_rewrite,
            critic_model=critic_model,
            rewrite_model=rewrite_model,
            json_domain=json_domain,
            actor=actor,
            principal=principal,
        )

    def retrieve(
        self, query: str, limit: int = 10, explain: bool = False, weights: RetrievalWeights | None = None
    ) -> list[HybridCandidate]:
        return hybrid_search_chunks(self.cfg, query, limit=limit, explain=explain, weights=weights)

    def retrieve_planned(
        self,
        query: str,
        limit: int = 10,
        as_of: str | None = None,
        weights: RetrievalWeights | None = None,
        actor: str = ACTOR_CLI,
    ) -> RetrievalPlanResult:
        """
        Retrieval planner v3 (Implementation Brief III, Phase E) --
        additive to retrieve() above, which stays exactly as v2 always
        was. A separate method, not a `plan=True` flag on retrieve(),
        since the two return entirely different shapes.
        """
        return plan_and_retrieve(self.cfg, query, limit=limit, as_of=as_of, actor=actor, weights=weights)

    def retrieval_plan_preview(self, query: str, as_of: str | None = None) -> RetrievalPlan:
        """
        Classification only, no actual chunk/vector search -- which of the
        planner's real strategies would fire for `query` and why (or why
        not), cheaper than a full retrieve_planned() call. plan_and_retrieve
        already calls build_retrieval_plan internally to decide what to
        run; this exposes that same real classification standalone for the
        PLANNER screen's own preview (Implementation Brief IV, Phase UI-5
        item 8).
        """
        return build_retrieval_plan(self.cfg, query, as_of=as_of)

    def evaluate_fragment(self, text: str) -> CycleEvaluation:
        """
        Runs an arbitrary candidate fragment through the real evaluator
        (evaluate.evaluate_cycle) standalone -- no cycle, no retrieval, no
        canonization -- for the EVALUATION lab (Implementation Brief IV,
        Phase UI-5 item 9). json_rows is deliberately [] (no retrieved
        evidence to cite in a standalone preview, unlike a real cycle);
        doctrinal_enforcement is scored honestly lower as a result, not
        faked. motif_counts still uses the real current canon window, so
        canon_loop_penalty reflects genuine corpus state.
        """
        return evaluate_cycle(text, json_rows=[], motif_counts=load_motif_counts(self.cfg), cfg=self.cfg)

    def canon(
        self,
        verdict: str = "CANON",
        include_retired: bool = False,
        limit: int = 50,
    ) -> list[CanonEntry]:
        query = "SELECT id, query, fragment, final_score, verdict, created_at, parent_cycle_ids FROM cycles WHERE dry_run = 0"
        params: list = []
        if verdict:
            query += " AND verdict = ?"
            params.append(verdict)
        if not include_retired:
            query += " AND retired_at IS NULL"
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with connect(self.cfg.database) as conn:
            rows = conn.execute(query, params).fetchall()

        return [_row_to_canon_entry(row) for row in rows]

    def canon_as_of_cycle(self, cycle_id: int) -> list[CanonEntry]:
        """Active canon reconstructed as of the moment `cycle_id` was itself created (temporal.canon_as_of_cycle)."""
        return _canon_entries_for_ids(self.cfg, _canon_as_of_cycle(self.cfg, cycle_id))

    def canon_as_of_timestamp(self, timestamp: str) -> list[CanonEntry]:
        """Active canon reconstructed as of an explicit timestamp -- valid-time travel (temporal.canon_valid_as_of)."""
        return _canon_entries_for_ids(self.cfg, _canon_valid_as_of(self.cfg, timestamp))

    def why_changed(self, cycle_id: int) -> WhyChangedResult:
        return _why_changed(self.cfg, cycle_id)

    def school_membership_as_of(self, timestamp: str) -> list[ActiveSchool]:
        return _school_membership_as_of(self.cfg, timestamp)

    def weather_as_of(self, timestamp: str, scope: str = SCOPE_CANON) -> dict[str, float]:
        return _weather_as_of(self.cfg, timestamp, scope=scope)

    def lineage(self, target_id: str) -> LineageResult:
        try:
            tree = build_lineage_tree(self.cfg, target_id)
        except LineageNotFoundError:
            raise

        stripped = target_id.strip()
        object_type = OBJECT_CYCLE if stripped.lstrip("-").isdigit() else OBJECT_JSON_ENTRY
        object_id: str | int = int(stripped) if object_type == OBJECT_CYCLE else stripped
        add_provenance_graph_section(tree, self.cfg, object_type, object_id)

        # file=StringIO so building a lineage payload (e.g. for the server,
        # on every request) never writes to the process's real stdout.
        console = Console(record=True, width=100, file=io.StringIO())
        console.print(tree)
        return LineageResult(target_id=target_id, text=console.export_text())

    def provenance(self, target_id: str) -> ProvenanceReport:
        return build_provenance_report_for_target(self.cfg, target_id)

    def weather(self, surface_limit: int = DEFAULT_SURFACE_LIMIT, history_limit: int = 30) -> WeatherSnapshot:
        canon_readings = corpus_weather(self.cfg, scope=SCOPE_CANON)
        surface_readings = corpus_weather(self.cfg, scope=SCOPE_SURFACE, surface_limit=surface_limit)
        surface_history = {h.axis: h.values for h in axis_history(self.cfg, SCOPE_SURFACE, limit=history_limit)}
        canon_history = {h.axis: h.values for h in axis_history(self.cfg, SCOPE_CANON, limit=history_limit)}
        return WeatherSnapshot(
            canon=canon_readings, surface=surface_readings, surface_history=surface_history, canon_history=canon_history
        )

    def weather_by_school(self) -> list[SchoolWeatherReading]:
        return [
            SchoolWeatherReading(
                school_id=school.id,
                name=school.name,
                readings=school_weather_profile(self.cfg, school_member_cycle_ids(self.cfg, school.id)),
            )
            for school in latest_schools(self.cfg)
        ]

    def schools(self) -> list[ActiveSchool]:
        return latest_schools(self.cfg)

    def schools_all_runs(self) -> list[SchoolRunEntry]:
        return all_schools(self.cfg)

    def school_members(self, school_id: int) -> list[SchoolMember]:
        return school_members_detail(self.cfg, school_id)

    def school_distances(self, run_at: str) -> list[SchoolDistance]:
        return school_distances_for_run(self.cfg, run_at)

    def run_schools_clustering(
        self,
        k: str | int = "auto",
        seed: int = 42,
        model: str | None = None,
        actor: str = ACTOR_CLI,
        correlation_id: str | None = None,
    ) -> list[SchoolResult]:
        return run_schools(self.cfg, k=k, seed=seed, model=model, actor=actor, correlation_id=correlation_id)

    def councils(self, limit: int = 20) -> list[CouncilSummary]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute(
                "SELECT id, started_at, finished_at, examined, overturned, notes FROM councils "
                "ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            CouncilSummary(
                council_id=int(row["id"]),
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                examined=int(row["examined"] or 0),
                overturned=int(row["overturned"] or 0),
                notes=row["notes"],
            )
            for row in rows
        ]

    def council_detail(self, council_id: int) -> CouncilDetail:
        return council_detail(self.cfg, council_id)

    def run_council(
        self,
        sample: int = 5,
        min_age_days: int = 7,
        audit_canon: bool = False,
        actor: str = ACTOR_CLI,
        principal: PrincipalContext | None = None,
    ):
        return run_council(
            self.cfg, sample=sample, min_age_days=min_age_days, audit_canon=audit_canon, actor=actor,
            principal=principal,
        )

    def fingerprint(self, source_id: int) -> SourceFingerprint | None:
        return get_or_compute_fingerprint(self.cfg, source_id)

    def stats(self) -> EngineStats:
        with connect(self.cfg.database) as conn:
            verdict_counts = {
                row["verdict"] or "unknown": int(row["n"])
                for row in conn.execute(
                    "SELECT verdict, COUNT(*) AS n FROM cycles WHERE dry_run = 0 AND retired_at IS NULL GROUP BY verdict"
                ).fetchall()
            }
            cycles_total = int(conn.execute("SELECT COUNT(*) AS n FROM cycles WHERE dry_run = 0").fetchone()["n"])
            sources_count = int(conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"])
            chunks_count = int(conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])
            councils_count = int(conn.execute("SELECT COUNT(*) AS n FROM councils").fetchone()["n"])
            schools_count = int(conn.execute("SELECT COUNT(DISTINCT run_at) AS n FROM schools").fetchone()["n"])
            json_entries_count = int(conn.execute("SELECT COUNT(*) AS n FROM json_entries").fetchone()["n"])
            tagged_chunks_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM ("
                    "SELECT chunk_id FROM chunk_concepts "
                    "UNION SELECT chunk_id FROM chunk_entities "
                    "UNION SELECT chunk_id FROM chunk_motifs"
                    ")"
                ).fetchone()["n"]
            )

        dream_dir = self.cfg.logs / "dreams"
        dream_runs_count = len(list(dream_dir.glob("dream_*.jsonl"))) if dream_dir.exists() else 0

        return EngineStats(
            cycles_total=cycles_total,
            canon_count=verdict_counts.get("CANON", 0),
            heresy_count=verdict_counts.get("HERESY", 0),
            useful_fragment_count=verdict_counts.get("USEFUL_FRAGMENT", 0),
            noise_count=verdict_counts.get("NOISE", 0),
            sources_count=sources_count,
            chunks_count=chunks_count,
            councils_count=councils_count,
            schools_count=schools_count,
            dream_runs_count=dream_runs_count,
            json_entries_count=json_entries_count,
            tagged_chunks_count=tagged_chunks_count,
        )

    # ---- Implementation Brief IV, Phase UI-2 item 1: application-service
    # consolidation. Every method below is a thin wrapper over an existing,
    # already-tested module -- no business logic added here, exactly the
    # discipline every method above this line already follows. Added so the
    # UI (and, from now on, the CLI/server) have one shared surface for
    # everything Phase UI-1's audit found present but not yet on the facade.

    def multi_cycle(
        self,
        query: str,
        model: str | None = None,
        agent_model: str | None = None,
        synthesizer_model: str | None = None,
        critic_model: str | None = None,
        rewrite_model: str | None = None,
        interpreter_model: str | None = None,
        dry_run: bool = False,
        auto_rewrite: bool = True,
        json_domain: str | None = None,
        actor: str = ACTOR_CLI,
        correlation_id: str | None = None,
    ) -> int:
        return run_multi_cycle(
            self.cfg,
            query,
            model=model,
            agent_model=agent_model,
            synthesizer_model=synthesizer_model,
            critic_model=critic_model,
            rewrite_model=rewrite_model,
            interpreter_model=interpreter_model,
            dry_run=dry_run,
            auto_rewrite=auto_rewrite,
            json_domain=json_domain,
            actor=actor,
            correlation_id=correlation_id,
        )

    def dream(
        self,
        budget_cycles: int,
        budget_minutes: float,
        seed: int | None = None,
        model: str | None = None,
        critic_model: str | None = None,
    ) -> DreamRun:
        return run_dream(
            self.cfg, budget_cycles, budget_minutes, seed=seed, model=model, critic_model=critic_model
        )

    def dream_runs(self) -> list[DreamRunSummary]:
        return list_dream_runs(self.cfg)

    def dream_run_events(self, stamp: str) -> list[dict] | None:
        return dream_run_events(self.cfg, stamp)

    def semantic_top_nodes(self, kind: str, limit: int = 20) -> list[SemanticNode]:
        return top_semantic_nodes(self.cfg, kind, limit=limit)

    def semantic_neighbors(self, kind: str, label: str, limit: int = 15) -> SemanticNeighborhood:
        return semantic_neighbors(self.cfg, kind, label, limit=limit)

    def proposals(self) -> list[ProposalSummary]:
        return list_proposals(self.cfg)

    def proposal(self, number: int) -> dict:
        return show_proposal(self.cfg, number)

    def proposal_apply(self, number: int, actor: str = ACTOR_CLI) -> Path:
        """Merges the proposal into ontology.yaml, parity-checks, and git-commits -- see proposals.apply_proposal. Never call without explicit user confirmation upstream."""
        return apply_proposal(self.cfg, number, actor=actor)

    def proposal_reject(self, number: int, actor: str = ACTOR_CLI) -> Path:
        return reject_proposal(self.cfg, number, actor=actor)

    def registry_list(self, kind: str) -> list[dict]:
        return list_registry(self.cfg, kind)

    def registry_entry(self, kind: str, entry_id: str) -> dict | None:
        return show_registry_entry(self.cfg, kind, entry_id)

    def replay(self, cycle_id: int, model: str | None = None) -> ReplayResult:
        return replay_cycle(self.cfg, cycle_id, model=model)

    def replay_config_drift(self, cycle_id: int) -> ConfigReplayResult:
        return replay_cycle_level2(self.cfg, cycle_id)

    def replay_evidence(self, cycle_id: int) -> EvidenceReplayResult:
        return replay_cycle_level3(self.cfg, cycle_id)

    def replay_schools(self, run_id: str) -> SchoolsReplayResult:
        return replay_schools_level4(self.cfg, run_id)

    def replay_variant(self, cycle_id: int, variant_model: str) -> ComparativeReplayResult:
        return replay_cycle_level5(self.cfg, cycle_id, variant_model)

    def replay_historical_canon(
        self, cycle_id: int, as_of: str, model: str | None = None
    ) -> HistoricalCanonReplayResult:
        return replay_cycle_against_historical_canon(self.cfg, cycle_id, as_of, model=model)

    def provenance_backfill(self) -> BackfillReport:
        return backfill_provenance_edges(self.cfg)

    def canon_backfill(self) -> TemporalBackfillReport:
        return _backfill_canon_temporal_states(self.cfg)

    def events(
        self,
        run_id: str | None = None,
        cycle_id: int | None = None,
        event_type: str | None = None,
        since_id: int | None = None,
        limit: int = 200,
    ) -> list[DomainEvent]:
        return query_events(
            self.cfg, run_id=run_id, aggregate_id=cycle_id, event_type=event_type, since_id=since_id, limit=limit
        )

    def event(self, event_id: str) -> DomainEvent | None:
        return EventRepository(self.cfg).get(event_id)

    def ingest_books(self) -> int:
        return ingest_books(self.cfg)

    def ingest_json(self) -> int:
        return ingest_json_corpus(self.cfg)

    def ingest_manifest(self, actor: str = ACTOR_CLI, correlation_id: str | None = None) -> list[ManifestIngestResult]:
        return ingest_from_manifest(self.cfg, actor=actor, correlation_id=correlation_id)

    def manifest_entries(self) -> list[SourceManifestEntry]:
        """Read-only: data/sources.yaml stays the one place curation is edited (Phase UI-4 item 2's CORPUS manifest view)."""
        return load_manifest(manifest_path(self.cfg))

    def sources(self, limit: int = 200, offset: int = 0, q: str | None = None) -> list[SourceEntry]:
        # FABLE Sec.15: "ne pas transférer l'intégralité de la base au
        # frontend pour afficher un tableau" -- a corpus of thousands of
        # sources must never come back in one unbounded response. limit is
        # clamped rather than trusted from the request: a caller asking for
        # 10_000_000 still only gets the sane cap.
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        where = ""
        params: tuple[str, ...] = ()
        if q:
            needle = f"%{q}%"
            where = " WHERE s.title LIKE ? OR s.source_type LIKE ?"
            params = (needle, needle)
        with connect(self.cfg.database) as conn:
            rows = conn.execute(
                f"{_SOURCE_QUERY}{where} GROUP BY s.id ORDER BY s.id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [_row_to_source_entry(row) for row in rows]

    def sources_count(self, q: str | None = None) -> int:
        where = ""
        params: tuple[str, ...] = ()
        if q:
            needle = f"%{q}%"
            where = " WHERE title LIKE ? OR source_type LIKE ?"
            params = (needle, needle)
        with connect(self.cfg.database) as conn:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM sources{where}", params).fetchone()
        return int(row["n"])

    def cycle_detail(self, cycle_id: int) -> CycleDetail | None:
        with connect(self.cfg.database) as conn:
            row = conn.execute(
                "SELECT id, query, model, prompt, response, dry_run, verdict, final_score, fragment, "
                "parent_cycle_ids, retired_at, retirement_reason, created_at FROM cycles WHERE id = ?",
                (cycle_id,),
            ).fetchone()
        if row is None:
            return None
        return CycleDetail(
            cycle_id=int(row["id"]),
            query=row["query"],
            model=row["model"],
            prompt=row["prompt"],
            response=row["response"],
            dry_run=bool(row["dry_run"]),
            verdict=row["verdict"],
            final_score=float(row["final_score"]) if row["final_score"] is not None else None,
            fragment=row["fragment"],
            parent_cycle_ids=json.loads(row["parent_cycle_ids"]) if row["parent_cycle_ids"] else [],
            retired_at=row["retired_at"],
            retirement_reason=row["retirement_reason"],
            created_at=row["created_at"],
        )

    def cycle_evidence(self, cycle_id: int) -> list[CycleSourceEntry]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute(
                "SELECT source_kind, ref, content FROM cycle_sources WHERE cycle_id = ?", (cycle_id,)
            ).fetchall()
        return [CycleSourceEntry(source_kind=row["source_kind"], ref=row["ref"], content=row["content"]) for row in rows]

    def source(self, source_id: int) -> SourceEntry | None:
        with connect(self.cfg.database) as conn:
            row = conn.execute(f"{_SOURCE_QUERY} WHERE s.id = ? GROUP BY s.id", (source_id,)).fetchone()
        return _row_to_source_entry(row) if row is not None else None

    def source_chunks(self, source_id: int, limit: int = 200) -> list[ChunkEntry]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute(
                "SELECT id, chunk_index, canonical_ref, content, char_start, char_end FROM chunks "
                "WHERE source_id = ? ORDER BY chunk_index LIMIT ?",
                (source_id, limit),
            ).fetchall()
        return [
            ChunkEntry(
                id=int(row["id"]),
                chunk_index=int(row["chunk_index"]),
                canonical_ref=row["canonical_ref"],
                content=row["content"],
                char_start=row["char_start"],
                char_end=row["char_end"],
            )
            for row in rows
        ]

    def config(self) -> dict:
        """Raw config.yaml contents, re-read from disk -- for the SETTINGS screen's read/compare view. No secrets live in this file (audited, Phase UI-1)."""
        import yaml

        config_path = self.cfg.root / "config.yaml"
        if not config_path.exists():
            return {}
        return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
