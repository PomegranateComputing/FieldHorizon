export type StartupStepId =
  | "locating_configuration"
  | "opening_database"
  | "checking_schema"
  | "connecting_model_provider"
  | "loading_capabilities"
  | "starting_event_channel"
  | "ready";

/** FABLE Sec.10.1's own ordered list. */
export const STARTUP_STEP_ORDER: StartupStepId[] = [
  "locating_configuration",
  "opening_database",
  "checking_schema",
  "connecting_model_provider",
  "loading_capabilities",
  "starting_event_channel",
  "ready",
];

export type StepStatus = "pending" | "ok" | "degraded" | "error";

export interface StartupStep {
  id: StartupStepId;
  status: StepStatus;
  detail?: string;
  /** i18n key, not a raw string -- translated at render time by whichever component displays it. */
  remediationKey?: string;
}

export type ConnectionPhase = "connecting" | "connected" | "degraded" | "reconnecting" | "disconnected";

export interface ConnectionState {
  phase: ConnectionPhase;
  steps: StartupStep[];
  /** Reconnect attempt count; 0 outside of "reconnecting". */
  attempt: number;
}
