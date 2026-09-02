from __future__ import annotations

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class RetrievalWeightsModel(BaseModel):
    """Mirrors config.RetrievalWeights exactly. Used both as a real-config readout (SystemInfoResponse.default_weights) and as a per-request override (RetrieveRequest.weights, Phase UI-4 item 3) -- never persisted."""

    vector_similarity: float
    bm25: float
    domain_prior: float
    source_weight: float
    severity: float


class StatusResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
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
    tagged_chunks_count: int


class CanonEntryModel(BaseModel):
    cycle_id: int
    query: str
    fragment: str
    final_score: float
    verdict: str
    created_at: str
    parent_cycle_ids: list[int]


class CanonResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    entries: list[CanonEntryModel]


class LineageResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    target_id: str
    text: str


class ProvenanceEdgeModel(BaseModel):
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    relation_type: str
    confidence: float


class ProvenanceResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    object_type: str
    object_id: str
    supporting_evidence_count: int
    opposing_evidence_count: int
    circular_ancestry: bool
    synthetic_dependency_ratio: float
    completeness_score: float
    missing_links: list[str]
    weakest_link: ProvenanceEdgeModel | None
    model_registry_ids: list[str]


class SchoolWeatherModel(BaseModel):
    school_id: int
    name: str
    readings: dict[str, float]


class WeatherResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    canon: dict[str, float]
    surface: dict[str, float]
    surface_history: dict[str, list[float]]
    canon_history: dict[str, list[float]] = Field(default_factory=dict)
    by_school: list[SchoolWeatherModel] = Field(default_factory=list)


class SchoolModel(BaseModel):
    id: int
    name: str
    summary: str
    member_count: int
    previous_school_id: int | None


class SchoolsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    schools: list[SchoolModel]


class SchoolRunEntryModel(BaseModel):
    id: int
    name: str
    summary: str
    member_count: int
    previous_school_id: int | None
    run_at: str


class SchoolsAllRunsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    schools: list[SchoolRunEntryModel]


class SchoolMemberModel(BaseModel):
    cycle_id: int
    query: str
    fragment: str
    verdict: str
    created_at: str
    distance: float


class SchoolMembersResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    school_id: int
    members: list[SchoolMemberModel]


class SchoolDistanceModel(BaseModel):
    school_a_id: int
    school_b_id: int
    distance: float


class SchoolDistancesResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_at: str
    distances: list[SchoolDistanceModel]


class RunSchoolsRequest(BaseModel):
    k: str | int = "auto"
    seed: int = 42
    model: str | None = None
    correlation_id: str | None = None


class RunSchoolsResultModel(BaseModel):
    school_id: int
    name: str
    summary: str
    member_cycle_ids: list[int]
    previous_school_id: int | None


class RunSchoolsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    correlation_id: str
    schools: list[RunSchoolsResultModel]


class CouncilModel(BaseModel):
    council_id: int
    started_at: str
    finished_at: str | None
    examined: int
    overturned: int
    notes: str | None


class CouncilsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    councils: list[CouncilModel]


class CouncilCaseModel(BaseModel):
    cycle_id: int
    query: str
    verdict: str
    final_score: float


class CouncilDetailResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    council_id: int
    rehabilitated: list[CouncilCaseModel]
    retired: list[CouncilCaseModel]
    promoted: list[CouncilCaseModel]


class SourceModel(BaseModel):
    id: int
    title: str
    source_type: str
    manifest_id: str | None
    weight: float


class SourcesResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    sources: list[SourceModel]
    total: int


class RetrieveRequest(BaseModel):
    query: str
    limit: int = 10
    explain: bool = False
    # plan=True switches to the retrieval planner v3 path (plan_and_retrieve)
    # instead of plain hybrid v2 -- diversity annotation and exclusion
    # reasons only exist on that path. as_of only affects canon-genealogy
    # candidates, gathered solely when plan=True.
    plan: bool = False
    as_of: str | None = None
    weights: RetrievalWeightsModel | None = None


class ScoreComponentModel(BaseModel):
    raw: float
    weight: float
    contribution: float


class RetrieveCandidateModel(BaseModel):
    chunk_id: int
    canonical_ref: str
    content: str
    source_title: str
    source_type: str
    score: float
    components: dict[str, ScoreComponentModel] = Field(default_factory=dict)
    exclusion_reason: str | None = None


class CanonCandidateModel(BaseModel):
    """Canon-genealogy candidates: only ever populated when plan=True -- plain hybrid v2 has no concept of these."""

    cycle_id: int
    query: str
    fragment: str
    score: float
    components: dict[str, ScoreComponentModel] = Field(default_factory=dict)
    supporting_evidence_count: int = 0
    synthetic_dependency_ratio: float = 0.0
    exclusion_reason: str | None = None


class StrategyAvailabilityModel(BaseModel):
    name: str
    backed: bool
    reason: str


class RetrievalPlanPreviewRequest(BaseModel):
    query: str
    as_of: str | None = None


class RetrievalPlanPreviewResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    query: str
    strategies_selected: list[str]
    strategies_available: list[StrategyAvailabilityModel]
    classification: dict


class EvaluationPreviewRequest(BaseModel):
    text: str


class EvaluationPreviewResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    symbolic_density: float
    symbolic_density_method: str
    doctrinal_enforcement: float
    doctrinal_enforcement_method: str
    length_score: float
    structure_score: float
    stuffing_penalty: float
    generic_penalty: float
    final_score: float
    verdict: str
    notes: list[str]


class RetrieveResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    candidates: list[RetrieveCandidateModel]
    # Everything below stays empty/default when plan=False -- the v2 path
    # (candidates above) is otherwise byte-identical to before this item.
    used_planner: bool = False
    canon_candidates: list[CanonCandidateModel] = Field(default_factory=list)
    excluded_candidates: list[RetrieveCandidateModel] = Field(default_factory=list)
    excluded_canon_candidates: list[CanonCandidateModel] = Field(default_factory=list)
    constraints_applied: list[str] = Field(default_factory=list)
    strategies_selected: list[str] = Field(default_factory=list)
    classification: dict = Field(default_factory=dict)


class CycleRequest(BaseModel):
    query: str
    model: str | None = None
    dry_run: bool = False
    auto_rewrite: bool = False
    critic_model: str | None = None
    rewrite_model: str | None = None
    json_domain: str | None = None


class CycleResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    cycle_id: int


class MultiCycleRequest(BaseModel):
    """
    Only the parameters run_multi_cycle really accepts (Phase UI-4 item 4's
    CYCLES launcher: "expose only parameters the engine really accepts") --
    FABLE Sec.10.6's fuller field list (workspace, planner mode, context
    budget, seed, tags, notes, ...) has no real backing anywhere in this
    engine and is not represented here.
    """

    query: str
    model: str | None = None
    agent_model: str | None = None
    synthesizer_model: str | None = None
    critic_model: str | None = None
    rewrite_model: str | None = None
    interpreter_model: str | None = None
    dry_run: bool = False
    auto_rewrite: bool = True
    json_domain: str | None = None
    correlation_id: str | None = None


class MultiCycleResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    cycle_id: int
    correlation_id: str


class FingerprintResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    source_id: int
    embedding_version: str
    domain_distribution: dict[str, float]
    top_motifs: list[str]
    chunk_count: int


class FactionModel(BaseModel):
    name: str
    doctrine_summary: str
    size: int
    weather_profile: dict[str, float]


class FactionsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    factions: list[FactionModel]


class DoctrineModel(BaseModel):
    fragment: str
    score: float
    lineage_root_ids: list[int]


class DoctrinesResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    doctrines: list[DoctrineModel]


class ContradictionModel(BaseModel):
    source_id: str
    target_id: str
    pressure_type: str
    statement_a: str
    statement_b: str
    reason: str
    pressure_score: float


class ContradictionsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    contradictions: list[ContradictionModel]


class BeliefModel(BaseModel):
    school_name: str
    sample_fragments: list[str]


class BeliefsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    beliefs: list[BeliefModel]


class MetricsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    cycles_run: int
    canon_size: int
    heresy_size: int
    councils_run: int
    dream_runs: int
    server_requests_total: int
    retrieval_latency_p50_ms: float | None
    retrieval_latency_p95_ms: float | None


class SystemInfoResponse(BaseModel):
    """Backs the app shell's top bar (Phase UI-3 item 4), SYSTEM/COMMAND screens, and RETRIEVAL lab's weight sliders (Phase UI-4 item 3) -- reads straight off AppConfig, not a re-parse of config.yaml."""

    schema_version: int = SCHEMA_VERSION
    version: str
    default_model: str
    embedding_model: str
    ollama_base_url: str
    mode: str
    tone: str
    database_path: str
    default_weights: RetrievalWeightsModel


class ErrorResponse(BaseModel):
    """FABLE Sec.13's structured error model -- every error this API returns takes this shape, never a raw traceback."""
    schema_version: int = SCHEMA_VERSION
    code: str
    title: str
    detail: str
    remediation: str
    correlation_id: str


class CapabilityModel(BaseModel):
    status: str
    detail: str = ""


class CapabilitiesResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    capabilities: dict[str, CapabilityModel]


class HealthCheckModel(BaseModel):
    name: str
    status: str
    detail: str = ""


class HealthResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    status: str  # "ok" if every check is ok, "degraded" if any is degraded, "failed" if any is failed
    checks: list[HealthCheckModel]


class SourceDetailModel(BaseModel):
    id: int
    title: str
    source_type: str
    manifest_id: str | None
    weight: float
    path: str
    language: str
    created_at: str
    chunk_count: int


class SourceDetailResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    source: SourceDetailModel


class ChunkModel(BaseModel):
    id: int
    chunk_index: int
    canonical_ref: str
    content: str
    char_start: int | None
    char_end: int | None


class ChunksResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    chunks: list[ChunkModel]


class CycleDetailModel(BaseModel):
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


class CycleDetailResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    cycle: CycleDetailModel


class CycleSourceModel(BaseModel):
    source_kind: str
    ref: str
    content: str


class CycleEvidenceResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    evidence: list[CycleSourceModel]


class ConfigResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    config: dict


class EventModel(BaseModel):
    event_id: str
    event_type: str
    event_version: int
    occurred_at: str
    recorded_at: str
    actor: str
    aggregate_type: str
    aggregate_id: str | None
    correlation_id: str
    causation_id: str | None
    run_id: str
    payload: dict
    principal_id: str | None


class EventsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    events: list[EventModel]


class ProposalSummaryModel(BaseModel):
    number: int
    name: str
    motif: str
    count: int


class ProposalsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    proposals: list[ProposalSummaryModel]


class ProposalActionResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    number: int
    path: str


class DreamRunSummaryModel(BaseModel):
    stamp: str
    started_at: str
    finished_at: str
    budget_cycles: int
    budget_minutes: float
    iterations: int
    regions_explored: int
    proposals_written: int


class DreamRunsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    runs: list[DreamRunSummaryModel]


class DreamRunEventsResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    stamp: str
    events: list[dict]


class SemanticNodeModel(BaseModel):
    kind: str
    label: str
    count: int


class SemanticTopNodesResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    kind: str
    nodes: list[SemanticNodeModel]


class SemanticNeighborModel(BaseModel):
    kind: str
    label: str
    shared_chunk_count: int


class SemanticNeighborhoodResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    kind: str
    label: str
    neighbors: list[SemanticNeighborModel]
    total_neighbor_count: int


class RegistryEntriesResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    entries: list[dict]


class RegistryEntryResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    entry: dict


class WhyChangedTransitionModel(BaseModel):
    from_verdict: str | None
    to_verdict: str
    valid_from: str
    active: bool
    event_type: str | None
    edge_relation_type: str | None


class WhyChangedResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    cycle_id: int
    transitions: list[WhyChangedTransitionModel]


class IngestManifestRequest(BaseModel):
    """
    correlation_id is optional and client-minted: passing one lets the
    caller open GET /events/stream?run_id=<that id> *before* this
    (synchronous, potentially slow) call returns, to watch per-source
    progress live. Omitting it just means the server mints its own and the
    caller only learns it from the response, after the fact.
    """

    correlation_id: str | None = None


class IngestManifestEntryModel(BaseModel):
    manifest_id: str
    title: str
    status: str
    chunk_count: int
    source_id: int | None


class IngestManifestResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    correlation_id: str
    entries: list[IngestManifestEntryModel]


class ManifestEntryModel(BaseModel):
    """Mirrors manifest.SourceManifestEntry -- read-only view of data/sources.yaml (Phase UI-4 item 2). Curation stays in the file; this is not an edit surface."""

    id: str
    title: str
    path: str
    author: str | None
    year: int | None
    language: str
    license: str
    domain_hints: list[str]
    weight: float
    notes: str


class ManifestResponse(BaseModel):
    schema_version: int = SCHEMA_VERSION
    entries: list[ManifestEntryModel]
