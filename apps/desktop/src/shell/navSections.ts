export interface NavSection {
  id: string;
  label: string;
  path: string;
  /** Key into /capabilities; undefined means "always shown, no gating" (e.g. COMMAND, SETTINGS). */
  capabilityKey?: string;
}

/**
 * FABLE Sec.9's own list plus Brief IV's four additions (WEATHER, SCHOOLS,
 * COUNCILS, DREAMS), in the order specified for this item.
 */
export const NAV_SECTIONS: NavSection[] = [
  { id: "command", label: "COMMAND", path: "/command" },
  { id: "corpus", label: "CORPUS", path: "/corpus" },
  { id: "retrieval", label: "RETRIEVAL", path: "/retrieval", capabilityKey: "retrieval_planner" },
  { id: "planner", label: "PLANNER", path: "/planner", capabilityKey: "retrieval_planner" },
  { id: "cycles", label: "CYCLES", path: "/cycles" },
  { id: "semantics", label: "SEMANTICS", path: "/semantics" },
  { id: "canon", label: "CANON", path: "/canon", capabilityKey: "bitemporal_canon" },
  { id: "weather", label: "WEATHER", path: "/weather", capabilityKey: "weather" },
  { id: "schools", label: "SCHOOLS", path: "/schools", capabilityKey: "schools" },
  { id: "councils", label: "COUNCILS", path: "/councils", capabilityKey: "councils" },
  { id: "dreams", label: "DREAMS", path: "/dreams", capabilityKey: "dreams" },
  { id: "provenance", label: "PROVENANCE", path: "/provenance", capabilityKey: "provenance_graph" },
  { id: "contradictions", label: "CONTRADICTIONS", path: "/contradictions", capabilityKey: "contradictions" },
  { id: "evaluation", label: "EVALUATION", path: "/evaluation" },
  { id: "models", label: "MODELS", path: "/models", capabilityKey: "registries" },
  { id: "system", label: "SYSTEM", path: "/system" },
  { id: "settings", label: "SETTINGS", path: "/settings" },
];

export type NavVisibility = "visible" | "experimental" | "hidden";

/**
 * present -> visible; partial -> shown but marked experimental (FABLE Sec.9:
 * "masquées ou marquées expérimentales"); absent -> hidden; capabilities not
 * loaded yet, or no capabilityKey at all -> visible (nothing to gate on).
 */
export function resolveNavVisibility(
  section: NavSection,
  capabilities: Record<string, { status: string }> | null,
): NavVisibility {
  if (!section.capabilityKey || !capabilities) {
    return "visible";
  }
  const capability = capabilities[section.capabilityKey];
  if (!capability) {
    return "visible";
  }
  if (capability.status === "absent") {
    return "hidden";
  }
  if (capability.status === "partial") {
    return "experimental";
  }
  return "visible";
}
