import { describe, expect, it, vi } from "vitest";

import { fetchCycleBundle, renderCycleBundleMarkdown } from "./cycleBundle";

const {
  getConfigMock,
  getContradictionsMock,
  getCycleDetailMock,
  getCycleEvidenceMock,
  getEventsForCycleMock,
  getLineageMock,
  getProvenanceMock,
  getSystemInfoMock,
  postEvaluationPreviewMock,
} = vi.hoisted(() => ({
  getConfigMock: vi.fn(),
  getContradictionsMock: vi.fn(),
  getCycleDetailMock: vi.fn(),
  getCycleEvidenceMock: vi.fn(),
  getEventsForCycleMock: vi.fn(),
  getLineageMock: vi.fn(),
  getProvenanceMock: vi.fn(),
  getSystemInfoMock: vi.fn(),
  postEvaluationPreviewMock: vi.fn(),
}));

vi.mock("../api/client", () => ({
  getConfig: getConfigMock,
  getContradictions: getContradictionsMock,
  getCycleDetail: getCycleDetailMock,
  getCycleEvidence: getCycleEvidenceMock,
  getEventsForCycle: getEventsForCycleMock,
  getLineage: getLineageMock,
  getProvenance: getProvenanceMock,
  getSystemInfo: getSystemInfoMock,
  postEvaluationPreview: postEvaluationPreviewMock,
}));

const CYCLE = {
  cycle_id: 42,
  query: "what is the machine",
  model: "hermes3:8b",
  prompt: "prompt text",
  response: "response text",
  dry_run: false,
  verdict: "CANON",
  final_score: 0.87,
  fragment: "the resulting canon fragment",
  parent_cycle_ids: [10, 11],
  retired_at: null,
  retirement_reason: null,
  created_at: "2026-07-01T00:00:00",
};

describe("fetchCycleBundle (FABLE Sec.18: reproducible cycle bundle -- real data only, mislabeled sections called out)", () => {
  it("assembles every section from the real per-cycle data, gated on GET /cycles/{id}", async () => {
    getCycleDetailMock.mockResolvedValue({ schema_version: 1, cycle: CYCLE });
    getConfigMock.mockResolvedValue({ schema_version: 1, config: { field_horizon: { tone: "dark" } } });
    getCycleEvidenceMock.mockResolvedValue({
      schema_version: 1,
      evidence: [{ source_kind: "book", ref: "ref-1", content: "verbatim evidence" }],
    });
    getEventsForCycleMock.mockResolvedValue({
      schema_version: 1,
      events: [
        {
          event_id: "e1",
          event_type: "CycleCompleted",
          event_version: 1,
          occurred_at: "2026-07-01T00:00:01",
          recorded_at: "2026-07-01T00:00:01",
          actor: "cli",
          aggregate_type: "cycle",
          aggregate_id: "42",
          correlation_id: "corr-1",
          causation_id: null,
          run_id: "run-1",
          payload: {},
          principal_id: null,
        },
      ],
    });
    getProvenanceMock.mockResolvedValue({
      schema_version: 1,
      object_type: "cycle",
      object_id: "42",
      supporting_evidence_count: 3,
      opposing_evidence_count: 0,
      circular_ancestry: false,
      synthetic_dependency_ratio: 0.1,
      completeness_score: 0.9,
      missing_links: [],
      weakest_link: null,
      model_registry_ids: [],
    });
    getLineageMock.mockResolvedValue({ schema_version: 1, target_id: "42", text: "42 <- 10 <- 11" });
    getContradictionsMock.mockResolvedValue({ schema_version: 1, contradictions: [] });
    getSystemInfoMock.mockResolvedValue({
      schema_version: 1,
      version: "3.2.0",
      default_model: "hermes3:8b",
      embedding_model: "nomic-embed-text",
      ollama_base_url: "http://localhost:11434",
      mode: "canonical_synthesis",
      tone: "dark",
      database_path: "/db.sqlite3",
      default_weights: { vector_similarity: 0.35, bm25: 0.25, domain_prior: 0.2, source_weight: 0.15, severity: 0.05 },
    });
    postEvaluationPreviewMock.mockResolvedValue({
      schema_version: 1,
      symbolic_density: 0.5,
      symbolic_density_method: "real",
      doctrinal_enforcement: 0.5,
      doctrinal_enforcement_method: "real",
      length_score: 0.5,
      structure_score: 0.5,
      stuffing_penalty: 0,
      generic_penalty: 0,
      final_score: 0.6,
      verdict: "CANON",
      notes: [],
    });

    const bundle = await fetchCycleBundle(42);

    expect(bundle.query).toBe("what is the machine");
    expect(bundle.model).toBe("hermes3:8b");
    expect(bundle.identifiers).toEqual({ cycle_id: 42, parent_cycle_ids: [10, 11] });
    expect(bundle.scores).toEqual({ final_score: 0.87, verdict: "CANON" });
    expect(bundle.evidence).toHaveLength(1);
    expect(bundle.events).toHaveLength(1);
    expect(bundle.configuration).toEqual({ field_horizon: { tone: "dark" } });
    // The two sections whose real per-cycle data was never persisted (or
    // never linked) must be labeled as such, not silently presented as
    // the original decision.
    expect(bundle.evaluation.recomputed_now).toBe(true);
    expect(bundle.contradictions.scoped_to_this_cycle).toBe(false);
    expect(postEvaluationPreviewMock).toHaveBeenCalledWith({ text: "the resulting canon fragment" });
  });
});

describe("renderCycleBundleMarkdown", () => {
  it("renders a human-readable bundle with the honest evaluation/contradictions caveats intact", () => {
    const markdown = renderCycleBundleMarkdown({
      configuration: {},
      query: "what is the machine",
      identifiers: { cycle_id: 42, parent_cycle_ids: [] },
      model: "hermes3:8b",
      evidence: [],
      scores: { final_score: 0.87, verdict: "CANON" },
      events: [],
      result: { fragment: "the fragment", response: "resp", prompt: "prompt", dry_run: false },
      evaluation: {
        recomputed_now: true,
        result: {
          schema_version: 1,
          symbolic_density: 0.5,
          symbolic_density_method: "real",
          doctrinal_enforcement: 0.5,
          doctrinal_enforcement_method: "real",
          length_score: 0.5,
          structure_score: 0.5,
          stuffing_penalty: 0,
          generic_penalty: 0,
          final_score: 0.6,
          verdict: "CANON",
          notes: [],
        },
      },
      provenance: {
        schema_version: 1,
        object_type: "cycle",
        object_id: "42",
        supporting_evidence_count: 0,
        opposing_evidence_count: 0,
        circular_ancestry: false,
        synthetic_dependency_ratio: 0,
        completeness_score: 1,
        missing_links: [],
        weakest_link: null,
        model_registry_ids: [],
      },
      contradictions: { scoped_to_this_cycle: false, snapshot: [] },
      canon_state: { verdict: "CANON", retired_at: null, retirement_reason: null },
      software_versions: { version: "3.2.0", default_model: "hermes3:8b", embedding_model: "nomic-embed-text" },
      lineage_text: "42",
    });

    expect(markdown).toContain("cycle #42");
    expect(markdown).toContain("recomputed now");
    expect(markdown).toContain("not scoped to this cycle");
  });
});
