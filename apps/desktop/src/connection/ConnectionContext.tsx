import { createContext, useContext, useEffect, useRef, useState } from "react";

import { getCapabilities, getHealth } from "../api/client";
import { getPlatform } from "../platform";
import type { ManagedBackendConfig } from "../platform";
import type { CapabilitiesResponse, HealthResponse } from "../api/types";
import type { ResourceState } from "../hooks/useApiResource";
import { deriveStartupSteps } from "./deriveStartupSteps";
import type { ConnectionState } from "./types";

const POLL_INTERVAL_MS = 10_000;
const BACKOFF_SCHEDULE_MS = [1000, 2000, 4000, 8000, 16000, 30000];
/** After this many consecutive failed reconnect attempts, stop retrying automatically and wait for the user to ask again. */
const MAX_AUTO_RECONNECT_ATTEMPTS = 8;

function backoffDelay(attempt: number): number {
  return BACKOFF_SCHEDULE_MS[Math.min(attempt, BACKOFF_SCHEDULE_MS.length - 1)];
}

interface ConnectionContextValue extends ConnectionState {
  retry: () => void;
  launchManagedBackend: (config: ManagedBackendConfig) => Promise<void>;
  stopManagedBackend: () => Promise<void>;
  /**
   * Stop then relaunch with whatever config launchManagedBackend was last
   * called with (Phase UI-6 item 5's command palette "Restart managed
   * backend" -- FABLE Sec.11's own example command). Throws if nothing has
   * been launched yet this session; canRestartManagedBackend below is the
   * honest guard the palette checks before even offering the command.
   */
  restartManagedBackend: () => Promise<void>;
  canRestartManagedBackend: boolean;
  /** null on the web build -- a browser tab cannot spawn a local process (FABLE Sec.8.4, Sec.14). */
  canManageBackend: boolean;
  /**
   * Raw health/capabilities, ResourceState-shaped so TopBar/SideNav/SystemPage
   * (Phase UI-3 item 4) can keep consuming the same shape they already do --
   * via this one shared poll, not each running its own independent one.
   */
  healthState: ResourceState<HealthResponse>;
  capabilitiesState: ResourceState<CapabilitiesResponse>;
}

const ConnectionContext = createContext<ConnectionContextValue | null>(null);

export function ConnectionProvider({ children }: { children: React.ReactNode }) {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<unknown>(null);
  const [capabilities, setCapabilities] = useState<CapabilitiesResponse | null>(null);
  const [capabilitiesError, setCapabilitiesError] = useState<unknown>(null);
  const [everConnected, setEverConnected] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [gaveUp, setGaveUp] = useState(false);
  const [canManageBackend, setCanManageBackend] = useState(false);

  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const capabilitiesRef = useRef<CapabilitiesResponse | null>(null);
  const lastLaunchConfigRef = useRef<ManagedBackendConfig | null>(null);
  const [canRestartManagedBackend, setCanRestartManagedBackend] = useState(false);

  useEffect(() => {
    getPlatform().then((platform) => {
      if (mountedRef.current) setCanManageBackend(platform.backend !== null);
    });
  }, []);

  function scheduleNext(delayMs: number) {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(poll, delayMs);
  }

  async function poll() {
    let healthOk = false;
    try {
      const data = await getHealth();
      if (!mountedRef.current) return;
      setHealth(data);
      setHealthError(null);
      healthOk = true;
    } catch (error) {
      if (!mountedRef.current) return;
      setHealthError(error);
      setHealth(null);
    }

    if (healthOk && !capabilitiesRef.current) {
      try {
        const data = await getCapabilities();
        if (mountedRef.current) {
          capabilitiesRef.current = data;
          setCapabilities(data);
          setCapabilitiesError(null);
        }
      } catch (error) {
        if (mountedRef.current) setCapabilitiesError(error);
      }
    }

    if (!mountedRef.current) return;

    if (healthOk) {
      setEverConnected(true);
      setGaveUp(false);
      setAttempt(0);
      scheduleNext(POLL_INTERVAL_MS);
      return;
    }

    setAttempt((prev) => {
      const next = prev + 1;
      if (next >= MAX_AUTO_RECONNECT_ATTEMPTS) {
        setGaveUp(true);
        // No further scheduleNext -- retry() below is the only way back in.
      } else {
        scheduleNext(backoffDelay(prev));
      }
      return next;
    });
  }

  useEffect(() => {
    mountedRef.current = true;
    poll();
    return () => {
      mountedRef.current = false;
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
    // Runs once: poll() re-schedules itself via scheduleNext, it isn't re-invoked by a dependency change.
  }, []);

  function retry() {
    setGaveUp(false);
    setAttempt(0);
    poll();
  }

  async function launchManagedBackend(config: ManagedBackendConfig): Promise<void> {
    const platform = await getPlatform();
    if (!platform.backend) {
      throw new Error("This build cannot manage a backend process.");
    }
    await platform.backend.launch(config);
    lastLaunchConfigRef.current = config;
    setCanRestartManagedBackend(true);
    retry();
  }

  async function stopManagedBackend(): Promise<void> {
    const platform = await getPlatform();
    if (!platform.backend) {
      throw new Error("This build cannot manage a backend process.");
    }
    await platform.backend.stop();
  }

  async function restartManagedBackend(): Promise<void> {
    const config = lastLaunchConfigRef.current;
    if (!config) {
      throw new Error("No managed backend has been launched this session -- nothing to restart.");
    }
    await stopManagedBackend();
    await launchManagedBackend(config);
  }

  const steps = deriveStartupSteps(health, healthError, capabilities, capabilitiesError);

  let phase: ConnectionState["phase"];
  if (!everConnected) {
    phase = "connecting";
  } else if (gaveUp) {
    phase = "disconnected";
  } else if (healthError) {
    phase = "reconnecting";
  } else if (health && health.status !== "ok") {
    phase = "degraded";
  } else {
    phase = "connected";
  }

  const healthState: ResourceState<HealthResponse> = health
    ? { status: "loaded", data: health }
    : healthError
      ? { status: "error", error: healthError }
      : { status: "loading" };

  const capabilitiesState: ResourceState<CapabilitiesResponse> = capabilities
    ? { status: "loaded", data: capabilities }
    : capabilitiesError
      ? { status: "error", error: capabilitiesError }
      : { status: "loading" };

  const value: ConnectionContextValue = {
    phase,
    steps,
    attempt,
    retry,
    launchManagedBackend,
    stopManagedBackend,
    restartManagedBackend,
    canRestartManagedBackend,
    canManageBackend,
    healthState,
    capabilitiesState,
  };

  return <ConnectionContext.Provider value={value}>{children}</ConnectionContext.Provider>;
}

export function useConnection(): ConnectionContextValue {
  const value = useContext(ConnectionContext);
  if (!value) {
    throw new Error("useConnection must be used within a ConnectionProvider");
  }
  return value;
}
