import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { ConnectionProvider, useConnection } from "./ConnectionContext";

const { getHealthMock, getCapabilitiesMock, getPlatformMock } = vi.hoisted(() => ({
  getHealthMock: vi.fn(),
  getCapabilitiesMock: vi.fn(),
  getPlatformMock: vi.fn(),
}));
vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, getHealth: getHealthMock, getCapabilities: getCapabilitiesMock };
});
vi.mock("../platform", () => ({ getPlatform: getPlatformMock }));

const OK_HEALTH = { schema_version: 1, status: "ok" as const, checks: [] };
const OK_CAPABILITIES = { schema_version: 1, capabilities: {} };

describe("ConnectionContext's real poll/backoff state machine (Phase UI-6 item 4: backend killed mid-cycle)", () => {
  it("goes connected -> reconnecting -> connected again when the backend drops and comes back", async () => {
    getPlatformMock.mockResolvedValue({ kind: "web", backend: null });
    getCapabilitiesMock.mockResolvedValue(OK_CAPABILITIES);
    // First poll succeeds (real connection established), second poll fails
    // (the backend process died), third poll succeeds again (it was
    // restarted) -- exactly FABLE's "backend killed mid-cycle... reconnects."
    getHealthMock
      .mockResolvedValueOnce(OK_HEALTH)
      .mockRejectedValueOnce(new ApiError(0, "FH_UNREACHABLE", "backend unreachable"))
      .mockResolvedValue(OK_HEALTH);

    const { result } = renderHook(() => useConnection(), { wrapper: ConnectionProvider });

    await waitFor(() => expect(result.current.phase).toBe("connected"));

    // The next poll (10s later in real operation) is the one that fails --
    // trigger it directly via retry() rather than waiting out POLL_INTERVAL_MS.
    result.current.retry();
    await waitFor(() => expect(result.current.phase).toBe("reconnecting"), { timeout: 3000 });
    expect(result.current.attempt).toBeGreaterThan(0);

    // Backoff schedules the next poll automatically (1000ms for attempt 1) --
    // no manual retry() needed here, matching real auto-reconnect behavior.
    await waitFor(() => expect(result.current.phase).toBe("connected"), { timeout: 3000 });
    expect(result.current.attempt).toBe(0);
  });

  it("gives up after MAX_AUTO_RECONNECT_ATTEMPTS and only reconnects again via a manual retry()", async () => {
    vi.useFakeTimers();
    try {
      getPlatformMock.mockResolvedValue({ kind: "web", backend: null });
      getCapabilitiesMock.mockResolvedValue(OK_CAPABILITIES);
      getHealthMock.mockResolvedValueOnce(OK_HEALTH).mockRejectedValue(new ApiError(0, "FH_UNREACHABLE", "gone"));

      const { result } = renderHook(() => useConnection(), { wrapper: ConnectionProvider });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(result.current.phase).toBe("connected");

      await act(async () => {
        result.current.retry();
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(result.current.attempt).toBe(1);

      // Backoff schedule is 1s,2s,4s,8s,16s,30s,30s,30s -- step through each
      // scheduled delay individually so the fake clock has a clean trigger
      // point every time, rather than one giant jump.
      const backoffScheduleMs = [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000];
      for (const delayMs of backoffScheduleMs) {
        if (result.current.phase === "disconnected") break;
        await act(async () => {
          await vi.advanceTimersByTimeAsync(delayMs);
        });
      }

      expect(result.current.phase).toBe("disconnected");
      expect(result.current.attempt).toBeGreaterThanOrEqual(8);

      // Manual retry is the only way back in once given up.
      getHealthMock.mockResolvedValue(OK_HEALTH);
      await act(async () => {
        result.current.retry();
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(result.current.phase).toBe("connected");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("ConnectionContext's managed-backend restart (Phase UI-6 item 5: command palette 'Restart managed backend')", () => {
  it("canRestartManagedBackend stays false until a launch actually succeeds, then restart stops and relaunches with the same config", async () => {
    const launch = vi.fn().mockResolvedValue(undefined);
    const stop = vi.fn().mockResolvedValue(undefined);
    getPlatformMock.mockResolvedValue({ kind: "tauri", backend: { launch, stop, isManaged: vi.fn() } });
    getHealthMock.mockResolvedValue(OK_HEALTH);
    getCapabilitiesMock.mockResolvedValue(OK_CAPABILITIES);

    const { result } = renderHook(() => useConnection(), { wrapper: ConnectionProvider });
    await waitFor(() => expect(result.current.phase).toBe("connected"));
    expect(result.current.canRestartManagedBackend).toBe(false);

    const config = { pythonBin: "/usr/bin/python3", repoRoot: "/repo", port: 8777, tokenFile: "/repo/.fh_token" };
    await act(async () => {
      await result.current.launchManagedBackend(config);
    });
    expect(launch).toHaveBeenCalledWith(config);
    expect(result.current.canRestartManagedBackend).toBe(true);

    await act(async () => {
      await result.current.restartManagedBackend();
    });
    expect(stop).toHaveBeenCalledOnce();
    expect(launch).toHaveBeenLastCalledWith(config);
  });

  it("restartManagedBackend refuses to run when nothing has been launched this session", async () => {
    getPlatformMock.mockResolvedValue({ kind: "tauri", backend: { launch: vi.fn(), stop: vi.fn(), isManaged: vi.fn() } });
    getHealthMock.mockResolvedValue(OK_HEALTH);
    getCapabilitiesMock.mockResolvedValue(OK_CAPABILITIES);

    const { result } = renderHook(() => useConnection(), { wrapper: ConnectionProvider });
    await waitFor(() => expect(result.current.phase).toBe("connected"));

    await expect(result.current.restartManagedBackend()).rejects.toThrow(/nothing to restart/i);
  });
});
