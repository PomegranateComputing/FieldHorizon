import type { EventEnvelope } from "../api/types";

/**
 * No endpoint currently reports "cycles in flight" (the engine's own
 * cycle_lock is internal, not surfaced via any GET route) -- rather than
 * inventing one, this derives it honestly from the live event stream
 * already flowing through the console: a CycleStarted with no matching
 * CycleCompleted/CycleFailed for the same run_id is still running.
 */
export function deriveActiveCycles(events: EventEnvelope[]): number {
  const started = new Set<string>();
  for (const event of events) {
    if (event.event_type === "CycleStarted") {
      started.add(event.run_id);
    } else if (event.event_type === "CycleCompleted" || event.event_type === "CycleFailed") {
      started.delete(event.run_id);
    }
  }
  return started.size;
}
