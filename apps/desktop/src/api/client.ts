import { getStoredToken } from "../auth/tokenStorage";
import type {
  CanonResponse,
  CapabilitiesResponse,
  ChunksResponse,
  ConfigResponse,
  ContradictionsResponse,
  CouncilDetailResponse,
  CouncilsResponse,
  CycleDetailResponse,
  CycleEvidenceResponse,
  DreamRunEventsResponse,
  DreamRunsResponse,
  ErrorResponse,
  EvaluationPreviewRequest,
  EvaluationPreviewResponse,
  EventsResponse,
  HealthResponse,
  IngestManifestResponse,
  LineageResponse,
  ManifestResponse,
  MetricsResponse,
  MultiCycleRequest,
  MultiCycleResponse,
  ProposalActionResponse,
  ProposalsResponse,
  ProvenanceResponse,
  RegistryEntriesResponse,
  RegistryEntryResponse,
  RetrievalPlanPreviewRequest,
  RetrievalPlanPreviewResponse,
  RetrieveRequest,
  RetrieveResponse,
  RunSchoolsRequest,
  RunSchoolsResponse,
  SchoolDistancesResponse,
  SchoolMembersResponse,
  SchoolsAllRunsResponse,
  SchoolsResponse,
  SemanticNeighborhoodResponse,
  SemanticTopNodesResponse,
  SourceDetailResponse,
  SourcesResponse,
  StatusResponse,
  SystemInfoResponse,
  WeatherResponse,
  WhyChangedResponse,
} from "./types";

/**
 * Always relative: same-origin once served at /ui in production (the whole
 * point of that static mount, Phase UI-3 item 2). During `npm run dev` the
 * frontend runs on its own Vite port (1420), separate from the backend --
 * vite.config.ts's server.proxy forwards these same relative paths to
 * localhost:8777 so the code here never needs to know which mode it's in.
 */
const BASE_URL = "";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public remediation: string,
    public detail: string = "",
  ) {
    super(`${code}: ${remediation}`.trim());
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getStoredToken();
  const headers = new Headers(init?.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(`${BASE_URL}${path}`, { ...init, headers });
  if (!response.ok) {
    const body: Partial<ErrorResponse> | null = await response.json().catch(() => null);
    throw new ApiError(
      response.status,
      body?.code ?? "FH_UNKNOWN_ERROR",
      body?.remediation ?? response.statusText,
      body?.detail ?? "",
    );
  }
  return (await response.json()) as T;
}

export function getHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/health");
}

export function getCapabilities(): Promise<CapabilitiesResponse> {
  return apiFetch<CapabilitiesResponse>("/capabilities");
}

export function getSystemInfo(): Promise<SystemInfoResponse> {
  return apiFetch<SystemInfoResponse>("/system-info");
}

export function getConfig(): Promise<ConfigResponse> {
  return apiFetch<ConfigResponse>("/config");
}

export function getStatus(): Promise<StatusResponse> {
  return apiFetch<StatusResponse>("/status");
}

/**
 * verdict="" is a real, already-supported query param combination (not a
 * hack): engine.canon()'s WHERE clause only filters `AND verdict = ?` when
 * verdict is truthy, so an empty string returns cycles of every verdict,
 * newest first -- exactly "recent cycles" for the COMMAND dashboard, with no
 * new backend route needed.
 */
export function getRecentCycles(limit = 10): Promise<CanonResponse> {
  const params = new URLSearchParams({ verdict: "", include_retired: "true", limit: String(limit) });
  return apiFetch<CanonResponse>(`/canon?${params}`);
}

export function getActiveCanon(limit = 100, includeRetired = false): Promise<CanonResponse> {
  const params = new URLSearchParams({ verdict: "CANON", include_retired: String(includeRetired), limit: String(limit) });
  return apiFetch<CanonResponse>(`/canon?${params}`);
}

export function getCanonAsOfCycle(cycleId: number): Promise<CanonResponse> {
  return apiFetch<CanonResponse>(`/canon?as_of_cycle=${cycleId}`);
}

export function getCanonAsOfTimestamp(timestamp: string): Promise<CanonResponse> {
  return apiFetch<CanonResponse>(`/canon?as_of_timestamp=${encodeURIComponent(timestamp)}`);
}

export function getWhyChanged(cycleId: number): Promise<WhyChangedResponse> {
  return apiFetch<WhyChangedResponse>(`/why-changed/${cycleId}`);
}

export function getSemanticTop(kind: string, limit = 20): Promise<SemanticTopNodesResponse> {
  return apiFetch<SemanticTopNodesResponse>(`/semantics/top?kind=${kind}&limit=${limit}`);
}

export function getSemanticNeighbors(kind: string, label: string, limit = 15): Promise<SemanticNeighborhoodResponse> {
  const params = new URLSearchParams({ kind, label, limit: String(limit) });
  return apiFetch<SemanticNeighborhoodResponse>(`/semantics/neighbors?${params}`);
}

export function getRecentEvents(limit = 20): Promise<EventsResponse> {
  return apiFetch<EventsResponse>(`/events?limit=${limit}`);
}

export function getEventsForCycle(cycleId: number, limit = 100): Promise<EventsResponse> {
  return apiFetch<EventsResponse>(`/events?cycle_id=${cycleId}&limit=${limit}`);
}

export function getRecentEventsByType(eventType: string, limit = 20): Promise<EventsResponse> {
  const params = new URLSearchParams({ event_type: eventType, limit: String(limit) });
  return apiFetch<EventsResponse>(`/events?${params}`);
}

export function getContradictions(): Promise<ContradictionsResponse> {
  return apiFetch<ContradictionsResponse>("/export/contradictions");
}

export function getWeather(): Promise<WeatherResponse> {
  return apiFetch<WeatherResponse>("/weather");
}

/** Point-in-time canon-scope-only readings (server.py's /weather as_of branch never populates surface/history/by_school). */
export function getWeatherAsOf(timestamp: string): Promise<WeatherResponse> {
  return apiFetch<WeatherResponse>(`/weather?as_of=${encodeURIComponent(timestamp)}`);
}

export function getSchools(): Promise<SchoolsResponse> {
  return apiFetch<SchoolsResponse>("/schools");
}

export function getCouncils(limit = 20): Promise<CouncilsResponse> {
  return apiFetch<CouncilsResponse>(`/councils?limit=${limit}`);
}

export function getCouncilDetail(councilId: number): Promise<CouncilDetailResponse> {
  return apiFetch<CouncilDetailResponse>(`/councils/${councilId}`);
}

export function getDreamRuns(): Promise<DreamRunsResponse> {
  return apiFetch<DreamRunsResponse>("/dreams");
}

export function getDreamRunEvents(stamp: string): Promise<DreamRunEventsResponse> {
  return apiFetch<DreamRunEventsResponse>(`/dreams/${encodeURIComponent(stamp)}`);
}

export function getProposals(): Promise<ProposalsResponse> {
  return apiFetch<ProposalsResponse>("/proposals");
}

export function getProposal(number: number): Promise<Record<string, unknown>> {
  return apiFetch<Record<string, unknown>>(`/proposals/${number}`);
}

export function postApplyProposal(number: number): Promise<ProposalActionResponse> {
  return apiFetch<ProposalActionResponse>(`/proposals/${number}/apply`, { method: "POST" });
}

export function postRejectProposal(number: number): Promise<ProposalActionResponse> {
  return apiFetch<ProposalActionResponse>(`/proposals/${number}/reject`, { method: "POST" });
}

export function getSchoolsAllRuns(): Promise<SchoolsAllRunsResponse> {
  return apiFetch<SchoolsAllRunsResponse>("/schools/all");
}

export function getSchoolMembers(schoolId: number): Promise<SchoolMembersResponse> {
  return apiFetch<SchoolMembersResponse>(`/schools/${schoolId}/members`);
}

export function getSchoolDistances(runAt: string): Promise<SchoolDistancesResponse> {
  return apiFetch<SchoolDistancesResponse>(`/schools/distances?run_at=${encodeURIComponent(runAt)}`);
}

export function postRunSchools(body: RunSchoolsRequest): Promise<RunSchoolsResponse> {
  return apiFetch<RunSchoolsResponse>("/schools/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function postPlannerPreview(body: RetrievalPlanPreviewRequest): Promise<RetrievalPlanPreviewResponse> {
  return apiFetch<RetrievalPlanPreviewResponse>("/planner/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function postEvaluationPreview(body: EvaluationPreviewRequest): Promise<EvaluationPreviewResponse> {
  return apiFetch<EvaluationPreviewResponse>("/evaluation/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function getRegistryList(kind: string): Promise<RegistryEntriesResponse> {
  return apiFetch<RegistryEntriesResponse>(`/registry/${kind}`);
}

export function getRegistryEntry(kind: string, entryId: string): Promise<RegistryEntryResponse> {
  return apiFetch<RegistryEntryResponse>(`/registry/${kind}/${encodeURIComponent(entryId)}`);
}

export function getMetrics(): Promise<MetricsResponse> {
  return apiFetch<MetricsResponse>("/metrics");
}

/**
 * Paginated + server-searched (FABLE Sec.15: a corpus of thousands of
 * sources must never come back as one unbounded transfer). signal is
 * threaded through so CorpusPage's live search can cancel a stale in-flight
 * request rather than risk an older response overwriting a newer one.
 */
export function getSources(
  params?: { limit?: number; offset?: number; q?: string },
  signal?: AbortSignal,
): Promise<SourcesResponse> {
  const search = new URLSearchParams();
  if (params?.limit !== undefined) search.set("limit", String(params.limit));
  if (params?.offset !== undefined) search.set("offset", String(params.offset));
  if (params?.q) search.set("q", params.q);
  const qs = search.toString();
  return apiFetch<SourcesResponse>(`/sources${qs ? `?${qs}` : ""}`, { signal });
}

export function getSourceDetail(id: number): Promise<SourceDetailResponse> {
  return apiFetch<SourceDetailResponse>(`/sources/${id}`);
}

export function getSourceChunks(id: number, limit = 200): Promise<ChunksResponse> {
  return apiFetch<ChunksResponse>(`/sources/${id}/chunks?limit=${limit}`);
}

export function getCycleDetail(cycleId: number): Promise<CycleDetailResponse> {
  return apiFetch<CycleDetailResponse>(`/cycles/${cycleId}`);
}

export function getCycleEvidence(cycleId: number): Promise<CycleEvidenceResponse> {
  return apiFetch<CycleEvidenceResponse>(`/cycles/${cycleId}/evidence`);
}

export function getManifest(): Promise<ManifestResponse> {
  return apiFetch<ManifestResponse>("/manifest");
}

export function postRetrieve(body: RetrieveRequest): Promise<RetrieveResponse> {
  return apiFetch<RetrieveResponse>("/retrieve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function getLineage(targetId: string): Promise<LineageResponse> {
  return apiFetch<LineageResponse>(`/lineage/${encodeURIComponent(targetId)}`);
}

export function getProvenance(targetId: string): Promise<ProvenanceResponse> {
  return apiFetch<ProvenanceResponse>(`/provenance/${encodeURIComponent(targetId)}`);
}

export function postMultiCycle(body: MultiCycleRequest): Promise<MultiCycleResponse> {
  return apiFetch<MultiCycleResponse>("/multi-cycle", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function postIngestManifest(correlationId: string): Promise<IngestManifestResponse> {
  return apiFetch<IngestManifestResponse>("/ingest/manifest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ correlation_id: correlationId }),
  });
}

export function apiUrl(path: string): string {
  return `${BASE_URL}${path}`;
}
