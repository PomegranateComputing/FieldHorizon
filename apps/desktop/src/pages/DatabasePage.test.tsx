import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DatabasePage } from "./DatabasePage";

const mocks = vi.hoisted(() => ({
  getDbTargets: vi.fn(),
  getDbWorkloads: vi.fn(),
  getDbSnapshots: vi.fn(),
  getDbOperations: vi.fn(),
  getDbOperation: vi.fn(),
  postDbOperation: vi.fn(),
  postDbDiscover: vi.fn(),
}));

vi.mock("../api/client", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api/client")>()), ...mocks }));

const DETAIL = {
  operation: {
    operation_id: "OP-1", kind: "index_optimization", target: "bench_local", status: "RETAINED", state: "finished",
    message: "bench.orders(customer_id) retained", started_at: "2026-11-03T15:00:00", finished_at: "2026-11-03T15:00:09",
  },
  request: {},
  decision: {
    outcome: "RETAIN",
    criteria: [{ name: "target_improved:orders_by_customer", status: "pass", detail: "62.5 -> 0.03 ms" }],
    queries: {
      orders_by_customer: { role: "read", weight: 50, baseline: { median: 62.5 }, after: { median: 0.03 }, median_ratio: 0.0005, p_faster: 1e-12, p_slower: 1 },
    },
    weighted: { before: 3426.8, after: 310.9, ratio: 0.091 },
  },
  steps: [{ seq: 1, step: "preflight", status: "completed", detail: { authority: "granted" } }],
  changes: [],
  artifacts: [{ name: "report.md", kind: "report", sha256: "a".repeat(64), bytes: 10, created_at: "x" }],
  candidates: [{ candidate_id: "C-1", key: "bench.orders(customer_id)", source: "advisor", rationale: "seq scan read 2000000 rows" }],
  verdicts: { "C-1": { key: "bench.orders(customer_id)", eligible: true, blocking: [], checks: [] } },
};

function renderPage() {
  mocks.getDbTargets.mockResolvedValue({
    targets_file: "t.yaml",
    targets: [{ name: "bench_local", engine: "postgresql", host: "127.0.0.1", port: 55432, dbname: "fh_bench", environment: "isolated_test",
      observer_user: "fh_observer", operator_user: "fh_operator", writable: true, reachable: true, server_version: 180006, identity_matches: true, detail: "" }],
  });
  mocks.getDbWorkloads.mockResolvedValue({ workloads: [{ name: "bench_orders", file: "bench_orders.yaml", queries: [] }] });
  mocks.getDbSnapshots.mockResolvedValue({ snapshots: [] });
  mocks.getDbOperations.mockResolvedValue({ running: null, operations: [DETAIL.operation] });
  mocks.getDbOperation.mockResolvedValue(DETAIL);
  mocks.postDbOperation.mockResolvedValue({ operation_id: "OP-1", status: "RUNNING" });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><DatabasePage /></QueryClientProvider>);
}

describe("DatabasePage", () => {
  it("launches an autonomous operation with the operator's extra candidates and shows its evidence", async () => {
    renderPage();
    expect((await screen.findAllByText("bench_local")).length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.getByRole("button", { name: /Lancer|Launch/ })).toBeEnabled());

    await userEvent.type(screen.getByPlaceholderText(/schema\.table/), "bench.orders(status)");
    await userEvent.click(screen.getByRole("button", { name: /Lancer|Launch/ }));
    expect(mocks.postDbOperation).toHaveBeenCalledWith({
      target: "bench_local", workload: "bench_orders.yaml", candidates: ["bench.orders(status)"], use_llm: false, llm_model: null,
    });

    expect(await screen.findByText(/seq scan read 2000000 rows/)).toBeInTheDocument();
    expect(screen.getByText(/target_improved:orders_by_customer/)).toBeInTheDocument();
    expect(screen.getByText(/report\.md -- sha256/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /preuves|evidence/i })).toBeInTheDocument();
  });
});
