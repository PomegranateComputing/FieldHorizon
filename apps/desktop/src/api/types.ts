import type { components } from "./generated";

/**
 * Shell-only shapes (Phase UI-3) stay hand-written below -- duplicating the
 * full generated contract would have been premature for a shell calling four
 * endpoints. Screen-level types (Phase UI-4 onward) alias the generated
 * schemas directly instead, per that phase's own "every screen uses
 * generated types" rule -- these can never drift from the real contract
 * silently, since CI fails if regenerating contract/types.gen.ts produces a
 * diff (Phase UI-2 item 4).
 */
export type StatusResponse = components["schemas"]["StatusResponse"];
export type CanonEntryModel = components["schemas"]["CanonEntryModel"];
export type CanonResponse = components["schemas"]["CanonResponse"];
export type EventModel = components["schemas"]["EventModel"];
export type EventsResponse = components["schemas"]["EventsResponse"];
export type ContradictionModel = components["schemas"]["ContradictionModel"];
export type ContradictionsResponse = components["schemas"]["ContradictionsResponse"];
export type WeatherResponse = components["schemas"]["WeatherResponse"];
export type SchoolWeatherModel = components["schemas"]["SchoolWeatherModel"];
export type SchoolModel = components["schemas"]["SchoolModel"];
export type SchoolsResponse = components["schemas"]["SchoolsResponse"];
export type SchoolRunEntryModel = components["schemas"]["SchoolRunEntryModel"];
export type SchoolsAllRunsResponse = components["schemas"]["SchoolsAllRunsResponse"];
export type SchoolMemberModel = components["schemas"]["SchoolMemberModel"];
export type SchoolMembersResponse = components["schemas"]["SchoolMembersResponse"];
export type SchoolDistanceModel = components["schemas"]["SchoolDistanceModel"];
export type SchoolDistancesResponse = components["schemas"]["SchoolDistancesResponse"];
export type RunSchoolsRequest = components["schemas"]["RunSchoolsRequest"];
export type RunSchoolsResponse = components["schemas"]["RunSchoolsResponse"];
export type SourceModel = components["schemas"]["SourceModel"];
export type SourcesResponse = components["schemas"]["SourcesResponse"];
export type SourceDetailModel = components["schemas"]["SourceDetailModel"];
export type SourceDetailResponse = components["schemas"]["SourceDetailResponse"];
export type ChunkModel = components["schemas"]["ChunkModel"];
export type ChunksResponse = components["schemas"]["ChunksResponse"];
export type CycleDetailModel = components["schemas"]["CycleDetailModel"];
export type CycleDetailResponse = components["schemas"]["CycleDetailResponse"];
export type CycleSourceModel = components["schemas"]["CycleSourceModel"];
export type CycleEvidenceResponse = components["schemas"]["CycleEvidenceResponse"];
export type ConfigResponse = components["schemas"]["ConfigResponse"];
export type ManifestEntryModel = components["schemas"]["ManifestEntryModel"];
export type ManifestResponse = components["schemas"]["ManifestResponse"];
export type IngestManifestEntryModel = components["schemas"]["IngestManifestEntryModel"];
export type IngestManifestResponse = components["schemas"]["IngestManifestResponse"];
export type RetrievalWeightsModel = components["schemas"]["RetrievalWeightsModel"];
export type RetrieveRequest = components["schemas"]["RetrieveRequest"];
export type RetrieveCandidateModel = components["schemas"]["RetrieveCandidateModel"];
export type CanonCandidateModel = components["schemas"]["CanonCandidateModel"];
export type RetrieveResponse = components["schemas"]["RetrieveResponse"];
export type MultiCycleRequest = components["schemas"]["MultiCycleRequest"];
export type MultiCycleResponse = components["schemas"]["MultiCycleResponse"];
export type CouncilModel = components["schemas"]["CouncilModel"];
export type CouncilsResponse = components["schemas"]["CouncilsResponse"];
export type CouncilCaseModel = components["schemas"]["CouncilCaseModel"];
export type CouncilDetailResponse = components["schemas"]["CouncilDetailResponse"];
export type ProposalSummaryModel = components["schemas"]["ProposalSummaryModel"];
export type ProposalsResponse = components["schemas"]["ProposalsResponse"];
export type ProposalActionResponse = components["schemas"]["ProposalActionResponse"];
export type DreamRunSummaryModel = components["schemas"]["DreamRunSummaryModel"];
export type DreamRunsResponse = components["schemas"]["DreamRunsResponse"];
export type DreamRunEventsResponse = components["schemas"]["DreamRunEventsResponse"];
export type WhyChangedTransitionModel = components["schemas"]["WhyChangedTransitionModel"];
export type WhyChangedResponse = components["schemas"]["WhyChangedResponse"];
export type SemanticNodeModel = components["schemas"]["SemanticNodeModel"];
export type SemanticTopNodesResponse = components["schemas"]["SemanticTopNodesResponse"];
export type SemanticNeighborModel = components["schemas"]["SemanticNeighborModel"];
export type SemanticNeighborhoodResponse = components["schemas"]["SemanticNeighborhoodResponse"];
export type StrategyAvailabilityModel = components["schemas"]["StrategyAvailabilityModel"];
export type RetrievalPlanPreviewRequest = components["schemas"]["RetrievalPlanPreviewRequest"];
export type RetrievalPlanPreviewResponse = components["schemas"]["RetrievalPlanPreviewResponse"];
export type MetricsResponse = components["schemas"]["MetricsResponse"];
export type RegistryEntriesResponse = components["schemas"]["RegistryEntriesResponse"];
export type RegistryEntryResponse = components["schemas"]["RegistryEntryResponse"];
export type EvaluationPreviewRequest = components["schemas"]["EvaluationPreviewRequest"];
export type EvaluationPreviewResponse = components["schemas"]["EvaluationPreviewResponse"];
export type LineageResponse = components["schemas"]["LineageResponse"];
export type ProvenanceResponse = components["schemas"]["ProvenanceResponse"];
export type ProvenanceEdgeModel = components["schemas"]["ProvenanceEdgeModel"];

export interface CapabilityModel {
  status: "present" | "partial" | "absent";
  detail: string;
}

export interface CapabilitiesResponse {
  schema_version: number;
  capabilities: Record<string, CapabilityModel>;
}

export interface HealthCheckModel {
  name: string;
  status: "ok" | "degraded" | "failed";
  detail: string;
}

export interface HealthResponse {
  schema_version: number;
  status: "ok" | "degraded" | "failed";
  checks: HealthCheckModel[];
}

export interface SystemInfoResponse {
  schema_version: number;
  version: string;
  default_model: string;
  embedding_model: string;
  ollama_base_url: string;
  mode: string;
  tone: string;
  database_path: string;
  default_weights: components["schemas"]["RetrievalWeightsModel"];
}

export interface ErrorResponse {
  schema_version: number;
  code: string;
  title: string;
  detail: string;
  remediation: string;
  correlation_id: string;
}

/** FABLE Sec.8.3's minimum SSE envelope, as fieldhorizon/server.py's _sse_envelope actually emits it. */
export interface EventEnvelope {
  event_id: string;
  run_id: string;
  cycle_id: string | null;
  timestamp: string;
  event_type: string;
  stage: string | null;
  severity: string | null;
  payload: Record<string, unknown>;
}
