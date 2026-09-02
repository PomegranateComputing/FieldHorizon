import { useEffect, useState } from "react";

export type ResourceState<T> = { status: "loading" } | { status: "error"; error: unknown } | { status: "loaded"; data: T };

/**
 * The small shared shape every top-bar/nav-gating fetch in the shell needs:
 * loading/error/loaded, optionally polled. Not FABLE Sec.17's full state
 * model (that's Phase UI-3 item 5) -- just enough for this item's shell to
 * avoid a blank page or an eternal spinner on its own three read-only calls.
 */
export function useApiResource<T>(fetcher: () => Promise<T>, pollIntervalMs?: number): ResourceState<T> {
  const [state, setState] = useState<ResourceState<T>>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const data = await fetcher();
        if (!cancelled) setState({ status: "loaded", data });
      } catch (error) {
        if (!cancelled) setState({ status: "error", error });
      }
    }

    load();
    if (!pollIntervalMs) {
      return () => {
        cancelled = true;
      };
    }
    const id = setInterval(load, pollIntervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
    // fetcher is deliberately not a dependency: callers pass a stable module-level
    // function (getHealth, getCapabilities, ...), so re-running on every render
    // would defeat the point of polling on an interval.
  }, [pollIntervalMs]);

  return state;
}
