import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";

import { useEventStreamContext } from "../shell/EventStreamContext";
import { dbApi } from "./api";

/**
 * Server state for the workstation. The overview polls (it carries live target
 * state); operation sheets poll only while their operation is live; every
 * domain event whose type starts with "Db" refreshes the db queries at once,
 * so the screen follows an operation without waiting for the next poll.
 */
export function useOverview() {
  return useQuery({ queryKey: ["db", "overview"], queryFn: dbApi.overview, refetchInterval: 4000 });
}

export function useOperation(id: string | undefined) {
  return useQuery({
    queryKey: ["db", "operation", id],
    queryFn: () => dbApi.operation(id as string),
    enabled: Boolean(id),
    refetchInterval: (query) => (query.state.data?.operation.live ? 1500 : false),
  });
}

export function useMeasurements(id: string | undefined, enabled: boolean, version: string | null | undefined) {
  return useQuery({ queryKey: ["db", "measurements", id, version], queryFn: () => dbApi.measurements(id as string), enabled: Boolean(id) && enabled });
}

export function useOperationsPage(params: Record<string, string | number | undefined>) {
  return useQuery({ queryKey: ["db", "operations", params], queryFn: () => dbApi.operations(params), placeholderData: keepPreviousData,
                    refetchInterval: 5000 });
}

export function useTargetDetail(name: string | undefined) {
  return useQuery({ queryKey: ["db", "target", name], queryFn: () => dbApi.target(name as string), enabled: Boolean(name), refetchInterval: 10000 });
}

export function usePolicy() {
  return useQuery({ queryKey: ["db", "policy"], queryFn: dbApi.policy, staleTime: 60_000 });
}

export function useSettings() {
  return useQuery({ queryKey: ["db", "settings"], queryFn: dbApi.settings, staleTime: 60_000 });
}

export function useWorkloads() {
  return useQuery({ queryKey: ["db", "workloads"], queryFn: dbApi.workloads, staleTime: 60_000 });
}

/** Refreshes db queries when a Db* domain event arrives (events are de-duplicated by the stream itself). */
export function useDbEventRefresh(): void {
  const { events } = useEventStreamContext();
  const client = useQueryClient();
  const seen = useRef(0);
  useEffect(() => {
    if (events.length <= seen.current) {
      seen.current = events.length;
      return;
    }
    const fresh = events.slice(seen.current);
    seen.current = events.length;
    if (fresh.some((e) => e.event_type.startsWith("Db"))) {
      void client.invalidateQueries({ queryKey: ["db"] });
    }
  }, [events, client]);
}

// --- the target the workstation is looking at ------------------------------------------------
const STORAGE_KEY = "field-horizon-db-target";

export interface TargetSelection {
  target: string | null;
  setTarget: (name: string) => void;
}

export const TargetContext = createContext<TargetSelection>({ target: null, setTarget: () => undefined });

export function useTargetSelectionState(available: string[]): TargetSelection {
  const [stored, setStored] = useState<string | null>(() => window.localStorage.getItem(STORAGE_KEY));
  const target = stored && available.includes(stored) ? stored : (available[0] ?? null);
  return useMemo(() => ({
    target,
    setTarget: (name: string) => {
      window.localStorage.setItem(STORAGE_KEY, name);
      setStored(name);
    },
  }), [target]);
}

export function useSelectedTarget(): TargetSelection {
  return useContext(TargetContext);
}
