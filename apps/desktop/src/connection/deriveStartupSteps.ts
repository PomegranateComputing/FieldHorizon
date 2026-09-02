import type { CapabilitiesResponse, HealthResponse } from "../api/types";
import { STARTUP_STEP_ORDER } from "./types";
import type { StartupStep } from "./types";

const CANNOT_REACH_BACKEND_REMEDIATION_KEY = "startup.cannotReachBackend";

/**
 * Maps the real, already-computed GET /health and GET /capabilities results
 * onto FABLE Sec.10.1's ordered startup narrative -- not a fake progress bar,
 * an honest re-presentation of the same checks the backend already runs on
 * every request. Nothing here is invented: if a step's real signal doesn't
 * exist yet (no response), it stays "pending", never a fabricated "ok".
 */
export function deriveStartupSteps(
  health: HealthResponse | null,
  healthError: unknown,
  capabilities: CapabilitiesResponse | null,
  capabilitiesError: unknown,
): StartupStep[] {
  if (!health) {
    return STARTUP_STEP_ORDER.map((id) => ({
      id,
      status: id === "locating_configuration" && healthError ? "error" : "pending",
      detail: id === "locating_configuration" && healthError ? String(healthError) : undefined,
      remediationKey: id === "locating_configuration" && healthError ? CANNOT_REACH_BACKEND_REMEDIATION_KEY : undefined,
    }));
  }

  const checkByName = new Map(health.checks.map((check) => [check.name, check]));
  const dbCheck = checkByName.get("db");
  const schemaCheck = checkByName.get("schema");
  const modelCheck = checkByName.get("model_provider");
  const eventCheck = checkByName.get("event_channel");

  const dbOk = dbCheck?.status === "ok";
  const schemaFailed = schemaCheck?.status === "failed";
  const schemaDegraded = schemaCheck?.status === "degraded";
  const eventOk = eventCheck?.status === "ok";
  const capabilitiesLoaded = capabilities !== null;

  const steps: StartupStep[] = [
    { id: "locating_configuration", status: "ok" },
    { id: "opening_database", status: dbOk ? "ok" : "error", detail: dbCheck?.detail },
    {
      id: "checking_schema",
      // "degraded" here means an old-but-usable schema (a migration is
      // needed, not that anything is broken) -- FABLE Sec.8.4's "signaler
      // les migrations nécessaires," surfaced via detail rather than
      // blocking startup outright.
      status: schemaFailed ? "error" : schemaDegraded ? "degraded" : "ok",
      detail: schemaCheck?.detail,
    },
    {
      id: "connecting_model_provider",
      // Never "error": the backend's own health check treats an unreachable
      // model provider as a soft dependency (registries._resolve_model_digest's
      // own fallback) -- the rest of the app works without it.
      status: modelCheck?.status === "degraded" ? "degraded" : "ok",
      detail: modelCheck?.detail,
    },
    {
      id: "loading_capabilities",
      status: capabilitiesError ? "error" : capabilitiesLoaded ? "ok" : "pending",
    },
    { id: "starting_event_channel", status: eventOk ? "ok" : "error", detail: eventCheck?.detail },
  ];

  const ready = dbOk && !schemaFailed && capabilitiesLoaded && eventOk;
  steps.push({ id: "ready", status: ready ? "ok" : "pending" });

  return steps;
}
