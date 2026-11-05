export interface NavSection {
  id: string;
  /** Engine vocabulary, never translated (COMMAND, CORPUS...); the workstation entries use labelKey instead. */
  label: string;
  /** i18n key for a translated label (the DB operations workstation). */
  labelKey?: string;
  path: string;
  /** "ops": the DB operations workstation (primary); "advanced": the corpus engine's screens. */
  group: "ops" | "advanced";
  /** Key into /capabilities; undefined means "always shown, no gating" (e.g. COMMAND, SETTINGS). */
  capabilityKey?: string;
  /** Exact-match highlighting (the workstation's home would otherwise be active on every /db route). */
  end?: boolean;
}

/** The DB operations workstation (DB-02): its own routes under /db, rendered by DbLayout. */
export const DB_SECTIONS: NavSection[] = [
  { id: "db", label: "Exploitation", labelKey: "ops.nav.exploitation", path: "/db", group: "ops", end: true },
  { id: "db-targets", label: "Cibles", labelKey: "ops.nav.targets", path: "/db/targets", group: "ops" },
  { id: "db-operations", label: "Opérations", labelKey: "ops.nav.operations", path: "/db/operations", group: "ops" },
  { id: "db-workloads", label: "Workloads", labelKey: "ops.nav.workloads", path: "/db/workloads", group: "ops" },
  { id: "db-evidence", label: "Preuves", labelKey: "ops.nav.evidence", path: "/db/evidence", group: "ops" },
  { id: "db-settings", label: "Paramètres", labelKey: "ops.nav.settings", path: "/db/settings", group: "ops" },
];

/**
 * UI specification Sec.9's own list plus Brief IV's four additions (WEATHER, SCHOOLS,
 * COUNCILS, DREAMS), in the order specified for this item.
 */
export const NAV_SECTIONS: NavSection[] = [
  { id: "command", label: "COMMAND", path: "/command", group: "advanced" },
  { id: "corpus", label: "CORPUS", path: "/corpus", group: "advanced" },
  { id: "retrieval", label: "RETRIEVAL", path: "/retrieval", capabilityKey: "retrieval_planner", group: "advanced" },
  { id: "planner", label: "PLANNER", path: "/planner", capabilityKey: "retrieval_planner", group: "advanced" },
  { id: "cycles", label: "CYCLES", path: "/cycles", group: "advanced" },
  { id: "semantics", label: "SEMANTICS", path: "/semantics", group: "advanced" },
  { id: "canon", label: "CANON", path: "/canon", capabilityKey: "bitemporal_canon", group: "advanced" },
  { id: "weather", label: "WEATHER", path: "/weather", capabilityKey: "weather", group: "advanced" },
  { id: "schools", label: "SCHOOLS", path: "/schools", capabilityKey: "schools", group: "advanced" },
  { id: "councils", label: "COUNCILS", path: "/councils", capabilityKey: "councils", group: "advanced" },
  { id: "dreams", label: "DREAMS", path: "/dreams", capabilityKey: "dreams", group: "advanced" },
  { id: "provenance", label: "PROVENANCE", path: "/provenance", capabilityKey: "provenance_graph", group: "advanced" },
  { id: "contradictions", label: "CONTRADICTIONS", path: "/contradictions", capabilityKey: "contradictions", group: "advanced" },
  { id: "evaluation", label: "EVALUATION", path: "/evaluation", group: "advanced" },
  { id: "models", label: "MODELS", path: "/models", capabilityKey: "registries", group: "advanced" },
  { id: "system", label: "SYSTEM", path: "/system", group: "advanced" },
  { id: "settings", label: "SETTINGS", path: "/settings", group: "advanced" },
];

export const ALL_SECTIONS: NavSection[] = [...DB_SECTIONS, ...NAV_SECTIONS];

export type NavVisibility = "visible" | "experimental" | "hidden";

/**
 * present -> visible; partial -> shown but marked experimental (UI specification Sec.9:
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
