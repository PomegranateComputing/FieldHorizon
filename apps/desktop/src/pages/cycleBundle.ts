import {
  getConfig,
  getContradictions,
  getCycleDetail,
  getCycleEvidence,
  getEventsForCycle,
  getLineage,
  getProvenance,
  getSystemInfo,
  getCycleEvaluation,
} from "../api/client";
import type {
  ConfigResponse,
  ContradictionModel,
  CycleDetailModel,
  CycleEvaluationResponse,
  CycleSourceModel,
  EventModel,
  ProvenanceResponse,
  SystemInfoResponse,
} from "../api/types";

/**
 * FABLE Sec.18's reproducible cycle bundle. Every section below maps to
 * something genuinely stored or computable for a real cycle.
 *
 * `evaluation` keeps three things apart (GET /cycles/{id}/evaluation):
 * the persisted decision (always original), the original per-component
 * breakdown recorded at decision time -- or, for cycles older than schema
 * version 7, an explicit "unavailable" instead of a reconstruction -- and a
 * recomputation in the cycle's own retrieval context, labeled with when and
 * with which evaluator version it ran. (The previous bundle re-scored the
 * text with no retrieved axioms at all, a different evaluation that turned a
 * persisted USEFUL_FRAGMENT into a "recomputed" CANON.)
 *
 * `contradictions` is the current live-computed corpus-wide snapshot, not
 * something scoped to this cycle specifically (no cycle/axiom linkage
 * exists for contradictions anywhere in the backend).
 */
export interface CycleBundle {
  configuration: ConfigResponse["config"];
  query: string;
  identifiers: { cycle_id: number; parent_cycle_ids: number[] };
  model: string;
  evidence: CycleSourceModel[];
  scores: { final_score: number | null; verdict: string | null };
  events: EventModel[];
  result: { fragment: string | null; response: string; prompt: string; dry_run: boolean };
  evaluation: CycleEvaluationResponse;
  provenance: ProvenanceResponse;
  contradictions: { scoped_to_this_cycle: false; snapshot: ContradictionModel[] };
  canon_state: { verdict: string | null; retired_at: string | null; retirement_reason: string | null };
  software_versions: Pick<SystemInfoResponse, "version" | "default_model" | "embedding_model">;
  lineage_text: string;
}

export async function fetchCycleBundle(cycleId: number): Promise<CycleBundle> {
  const cycle: CycleDetailModel = (await getCycleDetail(cycleId)).cycle;

  const [configRes, evidenceRes, eventsRes, provenanceRes, lineageRes, contradictionsRes, systemInfo, evaluation] =
    await Promise.all([
      getConfig(),
      getCycleEvidence(cycleId),
      getEventsForCycle(cycleId),
      getProvenance(String(cycleId)),
      getLineage(String(cycleId)),
      getContradictions(),
      getSystemInfo(),
      getCycleEvaluation(cycleId),
    ]);

  return {
    configuration: configRes.config,
    query: cycle.query,
    identifiers: { cycle_id: cycle.cycle_id, parent_cycle_ids: cycle.parent_cycle_ids },
    model: cycle.model,
    evidence: evidenceRes.evidence,
    scores: { final_score: cycle.final_score, verdict: cycle.verdict },
    events: eventsRes.events,
    result: { fragment: cycle.fragment, response: cycle.response, prompt: cycle.prompt, dry_run: cycle.dry_run },
    evaluation,
    provenance: provenanceRes,
    contradictions: { scoped_to_this_cycle: false, snapshot: contradictionsRes.contradictions },
    canon_state: { verdict: cycle.verdict, retired_at: cycle.retired_at, retirement_reason: cycle.retirement_reason },
    software_versions: {
      version: systemInfo.version,
      default_model: systemInfo.default_model,
      embedding_model: systemInfo.embedding_model,
    },
    lineage_text: lineageRes.text,
  };
}

export function renderCycleBundleMarkdown(bundle: CycleBundle): string {
  const lines: string[] = [];
  lines.push(`# Reproducible cycle bundle -- cycle #${bundle.identifiers.cycle_id}`);
  lines.push("");
  lines.push(`**Query:** ${bundle.query}`);
  lines.push(`**Model:** ${bundle.model}`);
  lines.push(`**Verdict:** ${bundle.scores.verdict ?? "--"} (score ${bundle.scores.final_score ?? "--"})`);
  lines.push(`**Parent cycles:** ${bundle.identifiers.parent_cycle_ids.join(", ") || "none"}`);
  lines.push("");
  lines.push("## Result");
  lines.push("");
  lines.push(bundle.result.fragment ?? "(no canon fragment -- not a CANON verdict)");
  lines.push("");
  lines.push("## Evidence");
  lines.push("");
  if (bundle.evidence.length === 0) {
    lines.push("_No evidence recorded for this cycle (older cycle, predates cycle_sources)._");
  } else {
    for (const e of bundle.evidence) {
      lines.push(`- **${e.source_kind}** \`${e.ref}\`: ${e.content.slice(0, 200)}`);
    }
  }
  lines.push("");
  lines.push("## Events");
  lines.push("");
  for (const event of bundle.events) {
    lines.push(`- ${event.occurred_at} -- ${event.event_type}`);
  }
  lines.push("");
  lines.push(...renderEvaluationMarkdown(bundle.evaluation));
  lines.push("## Provenance");
  lines.push("");
  lines.push(
    `Supporting evidence: ${bundle.provenance.supporting_evidence_count}, opposing: ${bundle.provenance.opposing_evidence_count}, completeness: ${bundle.provenance.completeness_score}`,
  );
  lines.push("");
  lines.push("### Lineage");
  lines.push("");
  lines.push("```");
  lines.push(bundle.lineage_text);
  lines.push("```");
  lines.push("");
  lines.push("## Contradictions (current corpus-wide snapshot, not scoped to this cycle)");
  lines.push("");
  lines.push(`${bundle.contradictions.snapshot.length} contradiction(s) in the live snapshot at export time.`);
  lines.push("");
  lines.push("## Software versions");
  lines.push("");
  lines.push(
    `Field Horizon ${bundle.software_versions.version}, model ${bundle.software_versions.default_model}, embeddings ${bundle.software_versions.embedding_model}`,
  );
  return lines.join("\n");
}

function formatComponents(components: Record<string, unknown>): string {
  return Object.entries(components)
    .filter(([key]) => key !== "notes")
    .map(([key, value]) => `${key}=${String(value)}`)
    .join(", ");
}

export function renderEvaluationMarkdown(evaluation: CycleEvaluationResponse): string[] {
  const lines: string[] = [];
  lines.push("## Evaluation");
  lines.push("");
  lines.push("### Persisted decision (original)");
  lines.push("");
  lines.push(`Verdict: ${evaluation.persisted_verdict ?? "--"}, final score: ${evaluation.persisted_final_score ?? "--"}`);
  lines.push("");
  lines.push("### Original components (recorded at decision time)");
  lines.push("");
  if (evaluation.original_status !== "recorded") {
    lines.push(`_Unavailable: ${evaluation.original_unavailable_reason ?? "not recorded"}_`);
  } else {
    for (const original of evaluation.original) {
      lines.push(
        `- **${original.role}**${original.selected ? " (selected)" : ""}: ${original.verdict} ${original.final_score} ` +
          `-- evaluator \`${original.evaluator_version}\`, recorded ${original.recorded_at}`,
      );
      lines.push(`  - ${formatComponents(original.components)}`);
    }
  }
  lines.push("");
  lines.push("### Recomputation (not the original decision)");
  lines.push("");
  const r = evaluation.recomputation;
  if (!r) {
    lines.push("_Not recomputed._");
  } else {
    const outcome = r.matches_persisted_decision ? "matches" : "DIFFERS FROM";
    const context =
      r.json_context_matches_original === null ? "unknown (no original record)" : r.json_context_matches_original ? "yes" : "NO";
    lines.push(
      `Recomputed ${r.recomputed_at} with evaluator \`${r.evaluator_version}\` in the cycle's own recorded retrieval ` +
        `context (${r.json_refs.length} axiom(s)${r.missing_json_refs.length ? `, missing now: ${r.missing_json_refs.join(", ")}` : ""}).`,
    );
    lines.push("");
    lines.push(`Result: ${r.verdict} ${r.final_score} -- ${outcome} the persisted decision (delta ${r.score_delta}).`);
    lines.push(`Axiom context identical to the original: ${context}.`);
    if (r.component_deltas && Object.keys(r.component_deltas).length > 0) {
      lines.push(`Component deltas vs original: ${JSON.stringify(r.component_deltas)}`);
    }
  }
  lines.push("");
  return lines;
}
