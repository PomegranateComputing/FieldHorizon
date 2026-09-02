import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { makeCapabilitiesResponse, makeHealthResponse, makeSystemInfoResponse } from "./test/fixtures";

const {
  getHealthMock,
  getCapabilitiesMock,
  getSystemInfoMock,
  getStatusMock,
  getRecentCyclesMock,
  getRecentEventsMock,
  getRecentEventsByTypeMock,
  getContradictionsMock,
  getWeatherMock,
  getSchoolsMock,
} = vi.hoisted(() => ({
  getHealthMock: vi.fn(),
  getCapabilitiesMock: vi.fn(),
  getSystemInfoMock: vi.fn(),
  getStatusMock: vi.fn(),
  getRecentCyclesMock: vi.fn(),
  getRecentEventsMock: vi.fn(),
  getRecentEventsByTypeMock: vi.fn(),
  getContradictionsMock: vi.fn(),
  getWeatherMock: vi.fn(),
  getSchoolsMock: vi.fn(),
}));
vi.mock("./api/client", () => ({
  getHealth: getHealthMock,
  getCapabilities: getCapabilitiesMock,
  getSystemInfo: getSystemInfoMock,
  getStatus: getStatusMock,
  getRecentCycles: getRecentCyclesMock,
  getRecentEvents: getRecentEventsMock,
  getRecentEventsByType: getRecentEventsByTypeMock,
  getContradictions: getContradictionsMock,
  getWeather: getWeatherMock,
  getSchools: getSchoolsMock,
  apiUrl: (path: string) => path,
}));

vi.mock("./platform", () => ({
  getPlatform: vi.fn().mockResolvedValue({ kind: "web", backend: null }),
  isTauri: () => false,
}));
vi.mock("./shell/EventStreamContext", () => ({
  EventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  useEventStreamContext: () => ({ events: [], connected: true }),
}));

function renderApp() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
}

describe("App shell against a mocked backend (Phase UI-3 item 6)", () => {
  beforeEach(() => {
    // App uses a real BrowserRouter, which reads/writes jsdom's actual
    // window.history -- that persists across tests in this file unless
    // reset, so a later test can inherit whatever path an earlier one
    // navigated to.
    window.history.pushState(null, "", "/");
    window.sessionStorage.setItem("field-horizon-api-token", "test-token");
    getSystemInfoMock.mockResolvedValue(makeSystemInfoResponse());
    // CommandPage (the default route) calls all of these -- harmless empty
    // defaults so tests that don't care about its content don't have to
    // stub every call individually.
    getStatusMock.mockResolvedValue({
      schema_version: 1,
      cycles_total: 0,
      canon_count: 0,
      heresy_count: 0,
      useful_fragment_count: 0,
      noise_count: 0,
      sources_count: 0,
      chunks_count: 0,
      councils_count: 0,
      schools_count: 0,
      dream_runs_count: 0,
      json_entries_count: 0,
      tagged_chunks_count: 0,
    });
    getRecentCyclesMock.mockResolvedValue({ schema_version: 1, entries: [] });
    getRecentEventsMock.mockResolvedValue({ schema_version: 1, events: [] });
    getRecentEventsByTypeMock.mockResolvedValue({ schema_version: 1, events: [] });
    getContradictionsMock.mockResolvedValue({ schema_version: 1, contradictions: [] });
    getWeatherMock.mockResolvedValue({ schema_version: 1, canon: {}, surface: {}, surface_history: {} });
    getSchoolsMock.mockResolvedValue({ schema_version: 1, schools: [] });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    window.sessionStorage.clear();
  });

  it("shows the startup screen, then the shell once /health and /capabilities resolve", async () => {
    getHealthMock.mockResolvedValue(makeHealthResponse());
    getCapabilitiesMock.mockResolvedValue(makeCapabilitiesResponse());

    renderApp();

    // The shell's nav landmark never appears before the connection is up.
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();

    // <Navigate> (the index route's redirect to /command) commits its own
    // location update in an effect, one render after the nav landmark first
    // appears -- both need waitFor, not just the first.
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("heading", { name: "COMMAND" })).toBeInTheDocument());
  });

  it("gates nav sections on the real /capabilities response: absent hidden, partial marked experimental", async () => {
    getHealthMock.mockResolvedValue(makeHealthResponse());
    getCapabilitiesMock.mockResolvedValue(makeCapabilitiesResponse({ weather: { status: "absent", detail: "" } }));

    renderApp();
    await waitFor(() => expect(screen.getByRole("navigation")).toBeInTheDocument());

    expect(screen.queryByText("WEATHER")).not.toBeInTheDocument();
    expect(screen.getByText("CONTRADICTIONS").closest("a")).toHaveTextContent("EXP");
  });

  it("shows an error step with no fabricated progress when the backend is unreachable", async () => {
    getHealthMock.mockRejectedValue(new Error("connect ECONNREFUSED"));

    renderApp();

    await waitFor(() => expect(screen.getByText(/ECONNREFUSED/)).toBeInTheDocument());
    // Nothing past the first step should ever claim success it hasn't earned.
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("shows the connection-loss banner over the still-visible shell once a previously healthy connection starts failing", async () => {
    // Fake timers must be installed *before* the component ever schedules its
    // real setTimeout -- adopting a real, already-pending timer isn't
    // possible, so installing this after the initial connect would leave the
    // recurring poll running on real time regardless of how far fake time is
    // advanced. Promise resolution (the mocked fetches below) isn't part of
    // the faked clock, so the initial connect still resolves normally.
    vi.useFakeTimers();
    getHealthMock.mockResolvedValueOnce(makeHealthResponse());
    getCapabilitiesMock.mockResolvedValue(makeCapabilitiesResponse());

    renderApp();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByRole("navigation")).toBeInTheDocument();

    // The steady-state poll only runs every 10s (ConnectionContext's POLL_INTERVAL_MS).
    getHealthMock.mockRejectedValue(new Error("connect ECONNREFUSED"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    vi.useRealTimers();

    expect(screen.getByText(/reconnex|reconnect/i)).toBeInTheDocument();
    // The shell (stale data) is still visible underneath the banner -- no blank page.
    expect(screen.getByRole("navigation")).toBeInTheDocument();
  });
});
