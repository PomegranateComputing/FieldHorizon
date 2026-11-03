/**
 * The DB operations workstation's API surface, typed from the frozen contract
 * (contract/openapi.json -> contract/types.gen.ts). Every call goes through the
 * authenticated client; nothing here builds SQL or accepts a file path.
 */
import { apiFetch, apiFetchResponse } from "../api/client";
import type { components } from "../api/generated";

type S = components["schemas"];
export type Overview = S["DbOverviewResponse"];
export type TargetCard = S["DbTargetCard"];
export type LiveState = S["DbLiveState"];
export type OperationSummary = S["DbOperationSummary"];
export type OperationsPage = S["DbOperationsPage"];
export type OperationView = S["DbOperationView"];
export type TargetDetail = S["DbTargetDetailResponse"];
export type Measurements = S["DbMeasurementsResponse"];
export type Verification = S["DbVerificationResponse"];
export type PolicyDoc = S["DbPolicyResponse"];
export type DbSettings = S["DbSettingsResponse"];
export type Workloads = S["DbWorkloadsResponse"];
export type LaunchRequest = S["DbOptimizeRequest"];
export type LaunchResponse = S["DbOptimizeResponse"];
export type ControlResponse = S["DbControlResponse"];
export type RecoverResponse = S["DbRecoverResponse"];
export type Detection = S["DbDetectionResponse"];
export type ControlAction = "pause" | "resume" | "cancel";

const enc = encodeURIComponent;

export const dbApi = {
  overview: () => apiFetch<Overview>("/db/overview"),
  target: (name: string) => apiFetch<TargetDetail>(`/db/targets/${enc(name)}`),
  incidents: (name: string) => apiFetch<Detection>(`/db/targets/${enc(name)}/incidents`),
  policy: () => apiFetch<PolicyDoc>("/db/policy"),
  settings: () => apiFetch<DbSettings>("/db/settings"),
  workloads: () => apiFetch<Workloads>("/db/workloads"),
  operations: (params: Record<string, string | number | undefined>) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    return apiFetch<OperationsPage>(`/db/operations-page?${query.toString()}`);
  },
  operation: (id: string) => apiFetch<OperationView>(`/db/operations/${enc(id)}/view`),
  measurements: (id: string) => apiFetch<Measurements>(`/db/operations/${enc(id)}/measurements`),
  verify: (id: string) => apiFetch<Verification>(`/db/operations/${enc(id)}/verify`),
  report: async (id: string) => (await apiFetchResponse(`/db/operations/${enc(id)}/report`)).text(),
  artifact: async (id: string, name: string) => (await apiFetchResponse(`/db/operations/${enc(id)}/artifacts/${enc(name)}`)).text(),
  evidence: async (id: string) => (await apiFetchResponse(`/db/operations/${enc(id)}/evidence`)).blob(),
  launch: (body: LaunchRequest) =>
    apiFetch<LaunchResponse>("/db/operations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  control: (id: string, action: ControlAction) =>
    apiFetch<ControlResponse>(`/db/operations/${enc(id)}/control`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }),
    }),
  recover: (target: string) =>
    apiFetch<RecoverResponse>("/db/recover", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target }) }),
};

/** A request id for one launch intent: kept until the launch succeeds, so a double click or a retry designates the same operation. */
export function newRequestId(): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `ui-${random}`.replace(/[^A-Za-z0-9_.:-]/g, "");
}
