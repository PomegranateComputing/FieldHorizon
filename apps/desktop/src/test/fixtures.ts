import type { CapabilitiesResponse, HealthResponse, SystemInfoResponse } from "../api/types";

export function makeHealthResponse(overrides: Partial<HealthResponse> = {}): HealthResponse {
  return {
    schema_version: 1,
    status: "ok",
    checks: [
      { name: "db", status: "ok", detail: "" },
      { name: "schema", status: "ok", detail: "version 4" },
      { name: "model_provider", status: "ok", detail: "" },
      { name: "event_channel", status: "ok", detail: "" },
    ],
    ...overrides,
  };
}

export function makeCapabilitiesResponse(overrides: Partial<CapabilitiesResponse["capabilities"]> = {}): CapabilitiesResponse {
  return {
    schema_version: 1,
    capabilities: {
      retrieval_planner: { status: "present", detail: "" },
      planner_modes: { status: "absent", detail: "No legacy/shadow/active modes exist." },
      contradictions: { status: "partial", detail: "Ontological pressure edges only." },
      provenance_graph: { status: "present", detail: "" },
      bitemporal_canon: { status: "present", detail: "" },
      domain_events: { status: "present", detail: "" },
      tiered_replay: { status: "present", detail: "" },
      registries: { status: "present", detail: "" },
      schools: { status: "present", detail: "" },
      weather: { status: "present", detail: "" },
      councils: { status: "present", detail: "" },
      dreams: { status: "present", detail: "" },
      ...overrides,
    },
  };
}

export function makeSystemInfoResponse(overrides: Partial<SystemInfoResponse> = {}): SystemInfoResponse {
  return {
    schema_version: 1,
    version: "3.2.0",
    default_model: "hermes3:8b",
    embedding_model: "nomic-embed-text",
    ollama_base_url: "http://localhost:11434",
    mode: "canonical_synthesis",
    tone: "dark",
    database_path: "/repo/data/field_horizon.sqlite3",
    default_weights: { vector_similarity: 0.35, bm25: 0.25, domain_prior: 0.2, source_weight: 0.15, severity: 0.05 },
    ...overrides,
  };
}
