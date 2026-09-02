import { useConnection } from "../connection/ConnectionContext";
import type { CapabilitiesResponse } from "../api/types";
import type { ResourceState } from "./useApiResource";

/** Phase UI-3 item 5: reads the one shared poll ConnectionProvider already runs, not a second independent one. */
export function useCapabilities(): ResourceState<CapabilitiesResponse> {
  return useConnection().capabilitiesState;
}
