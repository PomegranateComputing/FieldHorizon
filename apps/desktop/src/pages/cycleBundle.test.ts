import { describe, expect, it, vi } from "vitest";

import type { CycleEvaluationResponse } from "../api/types";
import { fetchCycleBundle, renderCycleBundleMarkdown, renderEvaluationMarkdown } from "./cycleBundle";

const {
  getConfigMock,
  getContradictionsMock,
  getCycleDetailMock,
  getCycleEvaluationMock,
  getCycleEvidenceMock,
  getEventsForCycleMock,
  getLineageMock,
  getProvenanceMock,
  getSystemInfoMock,
} = vi.hoisted(() => ({
  getConfigMock: vi.fn(),
  getContradictionsMock: vi.fn(),
  getCycleDetailMock: vi.fn(),
  getCycleEvaluationMock: vi.fn(),
  getCycleEvidenceMock: vi.fn(),
  getEventsForCycleMock: vi.fn(),
  getLineageMock: vi.fn(),
  getProvenanceMock: vi.fn(),
  getSystemInfoMock: vi.fn(),
}));

vi.mock("../api/client", () => ({
  getConfig: getConfigMock,
  getContradictions: getContradictionsMock,
  getCycleDetail: getCycleDetailMock,
  getCycleEvaluation: getCycleEvaluationMock,
  getCycleEvidence: getCycleEvidenceMock,
  getEventsForCycle: getEventsForCycleMock,
  getLineage: getLineageMock,
  getProvenance: getProvenanceMock,
  getSystemInfo: getSystemInfoMock,
}));

const CYCLE = {
  cycle_id: 42,
  query: "what is the machine",
  model: "hermes3:8b",
  prompt: "prompt text",
  response: "response text",
  dry_run: false,
  verdict: "USEFUL_FRAGMENT",
  final_score: 0.819,
  fragment: "the resulting fragment",
  parent_cycle_ids: [10, 11],
  retired_at: null,
  retirement_reason: null,
  created_at: "2026-07-01T00:00:00",
};

const COMPONENTS = {
  symbolic_density: 0.556,
  symbolic_density_method: "embedding",
  doctrinal_enforcement: 0.76,
  doctrinal_enforcement_method: "embedding",
  length_score: 1,
  structure_score: 1,
  stuffing_penalty: 0.082,
  generic_penalty: 0,
  notes: [],
  truncation_penalty: 0,
  canon_loop_penalty: 0,
  word_count: 250,
  paragraph_count: 4,
  canon_gate_passed: true,
};

const RECORDED: CycleEvaluationResponse = {
  schema_version: 1,
  cycle_id: 42,
  persisted_final_score: 0.819,
  persisted_verdict: "USEFUL_FRAGMENT",
  original_status: "recorded",
  original_unavailable_reason: null,
  original: [
    {
      role: "initial",
      selected: true,
      evaluator_version: "evaluate_cycle/1",
      final_score: 0.819,
      verdict: "USEFUL_FRAGMENT",
      components: COMPONENTS,
      context: { json_refs: ["tawhid_0001"] },
      recorded_at: "2026-11-03 14:14:45",
    },
  ],
  recomputation: {
    recomputed_at: "2026-11-03T15:00:00+00:00",
    evaluator_version: "evaluate_cycle/1",
    same_evaluator_version: true,
    json_refs: ["tawhid_0001"],
    missing_json_refs: [],
    json_context_sha256: "abc",
    json_context_matches_original: true,
    motif_counts_matches_original: true,
    final_score: 0.819,
    verdict: "USEFUL_FRAGMENT",
    components: COMPONENTS,
    matches_persisted_decision: true,
    score_delta: 0,
    component_deltas: {},
  },
};

const UNAVAILABLE: CycleEvaluationResponse = {
  ...RECORDED,
  original_status: "unavailable",
  original_unavailable_reason: "This cycle predates per-component evaluation persistence (schema version 7).",
  original: [],
  recomputation: { ...RECORDED.recomputation!, json_context_matches_original: null, component_deltas: null },
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
    getCycleEvaluationMock.mockResolvedValue(RECORDED);

    const bundle = await fetchCycleBundle(42);

    expect(bundle.query).toBe("what is the machine");
    expect(bundle.model).toBe("hermes3:8b");
    expect(bundle.identifiers).toEqual({ cycle_id: 42, parent_cycle_ids: [10, 11] });
    expect(bundle.scores).toEqual({ final_score: 0.819, verdict: "USEFUL_FRAGMENT" });
    expect(bundle.evidence).toHaveLength(1);
    expect(bundle.events).toHaveLength(1);
    expect(bundle.configuration).toEqual({ field_horizon: { tone: "dark" } });
    // The evaluation comes from the per-cycle record, not a context-free
    // re-score of the fragment text.
    expect(getCycleEvaluationMock).toHaveBeenCalledWith(42);
    expect(bundle.evaluation.original_status).toBe("recorded");
    expect(bundle.contradictions.scoped_to_this_cycle).toBe(false);
  });
});

describe("renderEvaluationMarkdown", () => {
  it("keeps the persisted decision, the original components and the recomputation apart", () => {
    const markdown = renderEvaluationMarkdown(RECORDED).join("\n");

    expect(markdown).toContain("### Persisted decision (original)");
    expect(markdown).toContain("Verdict: USEFUL_FRAGMENT, final score: 0.819");
    expect(markdown).toContain("**initial** (selected): USEFUL_FRAGMENT 0.819");
    expect(markdown).toContain("doctrinal_enforcement=0.76");
    expect(markdown).toContain("### Recomputation (not the original decision)");
    expect(markdown).toContain("matches the persisted decision");
  });

  it("says the original components are unavailable instead of presenting a recomputation as original", () => {
    const markdown = renderEvaluationMarkdown(UNAVAILABLE).join("\n");

    expect(markdown).toContain("_Unavailable: This cycle predates");
    expect(markdown).not.toContain("(selected)");
    expect(markdown).toContain("unknown (no original record)");
  });

  it("flags a recomputation that disagrees with the persisted decision", () => {
    const markdown = renderEvaluationMarkdown({
      ...RECORDED,
      recomputation: {
        ...RECORDED.recomputation!,
        final_score: 0.919,
        verdict: "CANON",
        matches_persisted_decision: false,
        score_delta: 0.1,
        component_deltas: { doctrinal_enforcement: 0.24 },
      },
    }).join("\n");

    expect(markdown).toContain("DIFFERS FROM the persisted decision (delta 0.1)");
    expect(markdown).toContain('"doctrinal_enforcement":0.24');
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
      scores: { final_score: 0.819, verdict: "USEFUL_FRAGMENT" },
      events: [],
      result: { fragment: "the fragment", response: "resp", prompt: "prompt", dry_run: false },
      evaluation: UNAVAILABLE,
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
      canon_state: { verdict: "USEFUL_FRAGMENT", retired_at: null, retirement_reason: null },
      software_versions: { version: "3.2.0", default_model: "hermes3:8b", embedding_model: "nomic-embed-text" },
      lineage_text: "42",
    });

    expect(markdown).toContain("cycle #42");
    expect(markdown).toContain("Recomputation (not the original decision)");
    expect(markdown).toContain("_Unavailable:");
    expect(markdown).toContain("not scoped to this cycle");
  });
});
