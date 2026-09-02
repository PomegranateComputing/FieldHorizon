import { createContext, useContext } from "react";

import { useEventStream } from "../hooks/useEventStream";
import type { EventStreamState } from "../hooks/useEventStream";

const EventStreamContext = createContext<EventStreamState | null>(null);

/** One SSE connection shared by the top bar (active-cycle count) and the console (log feed), not two. */
export function EventStreamProvider({ children }: { children: React.ReactNode }) {
  const state = useEventStream();
  return <EventStreamContext.Provider value={state}>{children}</EventStreamContext.Provider>;
}

export function useEventStreamContext(): EventStreamState {
  const state = useContext(EventStreamContext);
  if (!state) {
    throw new Error("useEventStreamContext must be used within an EventStreamProvider");
  }
  return state;
}
