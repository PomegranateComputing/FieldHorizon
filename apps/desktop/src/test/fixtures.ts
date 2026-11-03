import type { CapabilitiesResponse, HealthResponse, SystemInfoResponse } from "../api/types";

/** A minimal real-shaped /db/overview: one isolated, verified, idle target. */
export function makeDbOverview() {
  return {
    generated_at: "2026-11-03T20:00:00+00:00",
    policy: { version: 2, sha256: "a".repeat(64), source: "dbeng_policy.yaml", recovery_automatic: true, max_authorization_age_seconds: 300, orphan_wait_seconds: 60 },
    targets: [{
      name: "bench_local", engine: "postgresql", host: "127.0.0.1", port: 55432, dbname: "fh_bench", environment: "isolated_test", writable: true,
      data_classification: "synthetic", identities: { observer: "fh_observer", measure: "fh_measure", operator: "fh_operator", owner_role: "fh_bench_owner" },
      pinned: { instance_marker: "m-1", system_identifier: "42" }, fingerprint: "f".repeat(16), running_operation: null, reconciling: false, startup: null,
      live: { checked_at: "2026-11-03T20:00:00+00:00", reachable: true, identity_verified: true, server_version_num: 180006, server_version: "PostgreSQL 18.6",
              detection: { incidents: [], blocking: 0, lease: { lock_held: false, row: { fencing_token: 3 } } }, fieldhorizon_indexes: [], ledger: [], build_progress: [] },
    }],
    running: [],
    recent: [],
    recoveries: [],
    counts: {},
    startup: {},
  };
}

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
