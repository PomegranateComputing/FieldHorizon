import { useConnection } from "../connection/ConnectionContext";
import type { HealthResponse } from "../api/types";
import type { ResourceState } from "./useApiResource";

/** Phase UI-3 item 5: reads the one shared poll ConnectionProvider already runs, not a second independent one. */
export function useHealth(): ResourceState<HealthResponse> {
  return useConnection().healthState;
}
