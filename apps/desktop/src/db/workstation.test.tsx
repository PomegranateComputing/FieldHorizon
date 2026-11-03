import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../i18n";
import { makeDbOverview } from "../test/fixtures";
import { DbLayout } from "./DbLayout";
import { ExploitationPage } from "./ExploitationPage";
import { OperationSheet } from "./OperationSheet";
import { StatusChip } from "./status";

const api = vi.hoisted(() => ({
  overview: vi.fn(),
  workloads: vi.fn(),
  launch: vi.fn(),
  control: vi.fn(),
  operation: vi.fn(),
  measurements: vi.fn(),
  verify: vi.fn(),
  recover: vi.fn(),
}));
vi.mock("./api", async (importOriginal) => ({ ...(await importOriginal<typeof import("./api")>()), dbApi: api }));
vi.mock("../shell/EventStreamContext", () => ({ useEventStreamContext: () => ({ events: [], connected: true }) }));

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/db" element={<DbLayout />}>
            <Route index element={<ExploitationPage />} />
            <Route path="operations/:operationId" element={<OperationSheet />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const RETAINED_VIEW = {
  operation: { operation_id: "OP-1", kind: "index_optimization", target: "bench_local", status: "RETAINED", state: "finished", cause: "acceptance_passed",
    message: "bench.orders(customer_id) retained: every acceptance criterion passed.", started_at: "2026-11-03T20:00:00+00:00",
    finished_at: "2026-11-03T20:00:40+00:00", live: false, workload: "bench_orders", candidate: "bench.orders(customer_id)", outcome: "RETAIN",
    weighted_ratio: 0.099, recovery_mode: null, recovered_by: null, lease_token: 4, llm: false },
  request: {}, runtime: null, controls_available: [], controls: [], changes: [],
  timeline: [{ seq: 1, step: "preflight", status: "completed", started_at: "2026-11-03T20:00:00", finished_at: "2026-11-03T20:00:01", detail: { lease_token: 4 } }],
  decision: { outcome: "RETAIN", criteria: [{ name: "measurement_stable", status: "pass", detail: "control queries within 20%" }],
    weighted: { before: 3654.4, after: 360.3, ratio: 0.099 }, policy: { version: 2, sha256: "a" } },
  candidates: [], verdicts: {}, authority: null, authorization: null, change: null, compensation: null, final_state: null, claims: null,
  recovery: null, execution_failure: null, lease: null,
  artifacts: [{ name: "decision.json", kind: "decision", sha256: "b".repeat(64), bytes: 10, created_at: "x" }],
  report_available: true, measurements_available: false,
};

describe("DB operations workstation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    api.overview.mockResolvedValue(makeDbOverview());
    api.workloads.mockResolvedValue({ workloads: [{ name: "bench_orders", file: "bench_orders.yaml", version: 2,
      queries: [{ id: "q", kind: "read", weight: 1, sql: "SELECT 1", control: false }] }] });
  });

  it("shows the target's verified identity and policy in the context bar, from real overview data", async () => {
    renderAt("/db");
    const bar = await screen.findByRole("region", { name: "Contexte de la cible" });
    await waitFor(() => expect(bar).toHaveTextContent("bench_local"));
    expect(bar).toHaveTextContent("vérifiée");
    expect(bar).toHaveTextContent("v2");
  });

  it("launches once per intent: a double click sends one request id, then the sheet opens", async () => {
    let resolve: (v: unknown) => void = () => undefined;
    api.launch.mockImplementation(() => new Promise((r) => { resolve = r; }));
    api.operation.mockResolvedValue(RETAINED_VIEW);
    const user = userEvent.setup();
    renderAt("/db");
    const button = await screen.findByRole("button", { name: "Lancer" });
    await user.dblClick(button);
    expect(api.launch).toHaveBeenCalledTimes(1);
    const body = api.launch.mock.calls[0][0];
    expect(body).toMatchObject({ target: "bench_local", workload: "bench_orders.yaml", use_llm: false });
    expect(body.request_id).toMatch(/^ui-/);
    resolve({ operation_id: "OP-1", status: "RUNNING", created: true });
    await screen.findByRole("heading", { level: 1, name: /OP-1/ });
  });

  it("states every status in words, with a glyph, never by colour alone", () => {
    render(<StatusChip value="FAILED_NEEDS_RECOVERY" />);
    const chip = screen.getByTitle("FAILED_NEEDS_RECOVERY");
    expect(chip).toHaveTextContent("Échec, reprise nécessaire");
    expect(chip.querySelector("[aria-hidden='true']")?.textContent).toBe("✖");
  });

  it("asks for a real confirmation before cancelling, and Escape backs out", async () => {
    api.overview.mockResolvedValue({ ...makeDbOverview(), running: [{ ...RETAINED_VIEW.operation, status: "RUNNING", live: true, state: "measure", finished_at: null }] });
    const user = userEvent.setup();
    renderAt("/db");
    await user.click(await screen.findByRole("button", { name: /Annuler l'opération/ }));
    const dialog = screen.getByRole("dialog", { name: "Annuler cette opération ?" });
    expect(dialog).toHaveTextContent("prochain point sûr");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.control).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /Annuler l'opération/ }));
    api.control.mockResolvedValue({ operation_id: "OP-1", seq: 1, action: "cancel", semantics: "Takes effect at the next safe point." });
    await user.click(screen.getByRole("button", { name: "Confirmer l'annulation" }));
    expect(api.control).toHaveBeenCalledWith("OP-1", "cancel");
  });

  it("opens an operation sheet whose sections are keyboard-operable tabs", async () => {
    api.operation.mockResolvedValue(RETAINED_VIEW);
    api.verify.mockResolvedValue({ operation_id: "OP-1", checked_at: "x", attestation: { status: "none" },
      integrity: { all_intact: true, files: [{ name: "decision.json", integrity: "intact" }] },
      recomputation: { available: true, matches: true, recorded: { outcome: "RETAIN" }, recomputed: { outcome: "RETAIN" }, differences: [] } });
    const user = userEvent.setup();
    renderAt("/db/operations/OP-1");
    await screen.findByText("Conservé");
    const timeline = screen.getByRole("tab", { name: "Chronologie" });
    timeline.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Décision" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("measurement_stable");
    await user.click(screen.getByRole("tab", { name: "Preuves" }));
    await user.click(screen.getByRole("button", { name: "Vérifier" }));
    const panel = await screen.findByRole("region", { name: "Vérification" });
    expect(panel).toHaveTextContent("intacts");
    expect(panel).toHaveTextContent("aucune");
  });
});
