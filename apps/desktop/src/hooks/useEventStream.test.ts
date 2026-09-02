import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useEventStream } from "./useEventStream";

vi.mock("../auth/tokenStorage", () => ({ getStoredToken: () => "test-token" }));

/** A ReadableStream-like body that yields each frame string as its own chunk, then ends (or throws, simulating a mid-stream drop). */
function makeStreamBody(frames: string[], dropAfter?: number) {
  let i = 0;
  return {
    getReader() {
      return {
        async read() {
          if (dropAfter !== undefined && i >= dropAfter) {
            throw new Error("stream dropped");
          }
          if (i >= frames.length) return { done: true, value: undefined };
          const chunk = frames[i];
          i += 1;
          return { done: false, value: new TextEncoder().encode(chunk) };
        },
      };
    },
  };
}

function sseFrame(id: number, payload: object): string {
  return `id: ${id}\ndata: ${JSON.stringify(payload)}\n\n`;
}

describe("useEventStream reconnect + resume (Phase UI-6 item 4: backend killed mid-cycle)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("reconnects with since_id set to the last event received before the drop, and never re-delivers it", { timeout: 10_000 }, async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");

    // First connection: two events, then the stream breaks mid-read (the
    // backend process died mid-cycle).
    fetchMock.mockResolvedValueOnce({
      ok: true,
      body: makeStreamBody([sseFrame(1, { event: "a" }), sseFrame(2, { event: "b" })], 2),
    } as unknown as Response);

    // Reconnect: the backend is back up. Only events after id 2 should ever
    // be requested or appended -- no gap, no duplicate.
    fetchMock.mockResolvedValueOnce({
      ok: true,
      body: makeStreamBody([sseFrame(3, { event: "c" })]),
    } as unknown as Response);

    const { result } = renderHook(() => useEventStream());

    await waitFor(() => expect(result.current.events).toHaveLength(2));

    // Real time: the hook's first backoff delay is 1000ms before it
    // reconnects (fake timers don't mix cleanly with the raw
    // fetch+ReadableStream reading this hook does off the microtask queue).
    await waitFor(() => expect(result.current.events).toHaveLength(3), { timeout: 3000 });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const secondCallUrl = String(fetchMock.mock.calls[1][0]);
    expect(secondCallUrl).toContain("since_id=2");

    const eventIds = result.current.events.map((e) => (e as unknown as { event: string }).event);
    expect(eventIds).toEqual(["a", "b", "c"]);
  });
});
