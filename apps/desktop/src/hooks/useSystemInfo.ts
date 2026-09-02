import { getSystemInfo } from "../api/client";
import type { SystemInfoResponse } from "../api/types";
import { useApiResource } from "./useApiResource";
import type { ResourceState } from "./useApiResource";

export function useSystemInfo(): ResourceState<SystemInfoResponse> {
  return useApiResource(getSystemInfo);
}
