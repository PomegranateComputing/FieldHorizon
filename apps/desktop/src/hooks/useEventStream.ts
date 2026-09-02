import { useEffect, useState } from "react";

import { apiUrl } from "../api/client";
import { getStoredToken } from "../auth/tokenStorage";
import type { EventEnvelope } from "../api/types";

const MAX_BUFFERED_EVENTS = 500;
const BACKOFF_SCHEDULE_MS = [1000, 2000, 4000, 8000, 16000, 30000];

export interface EventStreamState {
  events: EventEnvelope[];
  connected: boolean;
}

/**
 * Consumes GET /events/stream by hand (fetch + ReadableStream), not the
 * browser's native EventSource -- EventSource can't set an Authorization
 * header, and every route here requires the bearer token. Parses the raw
 * "id: ...\ndata: ...\n\n" frames fieldhorizon/server.py's _sse_envelope
 * writes.
 *
 * Reconnects with backoff on drop (Phase UI-3 item 5, FABLE Sec.8.4), using
 * the last frame's real `id:` line as `since_id` on the next attempt --
 * genuine resumption from the append-only, gap-free event log, not a
 * from-scratch replay that would duplicate everything already shown. Kept
 * independent from ConnectionContext's own health-based reconnect loop: a
 * dropped stream and a failing health check are related but distinct real
 * failure modes (e.g. a hiccup on just this long-lived connection), each
 * self-healing on its own rather than one blocking on the other.
 */
export function useEventStream(): EventStreamState {
  const [events, setEvents] = useState<EventEnvelope[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    let timeoutId: ReturnType<typeof setTimeout> | null = null;
    let lastEventId: number | null = null;

    function scheduleReconnect(attempt: number) {
      if (cancelled) return;
      const delay = BACKOFF_SCHEDULE_MS[Math.min(attempt, BACKOFF_SCHEDULE_MS.length - 1)];
      timeoutId = setTimeout(() => {
        void connectOnce(attempt + 1);
      }, delay);
    }

    async function connectOnce(attempt: number) {
      if (cancelled) return;
      const token = getStoredToken();
      const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};
      const path = lastEventId !== null ? `/events/stream?since_id=${lastEventId}` : "/events/stream";

      let response: Response;
      try {
        response = await fetch(apiUrl(path), { headers, signal: controller.signal });
      } catch {
        scheduleReconnect(attempt);
        return;
      }
      if (cancelled) return;
      if (!response.ok || !response.body) {
        scheduleReconnect(attempt);
        return;
      }

      setConnected(true);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      try {
        while (!cancelled) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const frame of frames) {
            const lines = frame.split("\n");
            const idLine = lines.find((line) => line.startsWith("id: "));
            if (idLine) {
              const id = Number(idLine.slice("id: ".length));
              if (!Number.isNaN(id)) lastEventId = id;
            }
            const dataLine = lines.find((line) => line.startsWith("data: "));
            if (!dataLine) continue;
            try {
              const parsed = JSON.parse(dataLine.slice("data: ".length)) as EventEnvelope;
              setEvents((prev) => {
                const next = [...prev, parsed];
                return next.length > MAX_BUFFERED_EVENTS ? next.slice(next.length - MAX_BUFFERED_EVENTS) : next;
              });
            } catch {
              // Malformed frame -- skip rather than crash the whole stream.
            }
          }
        }
      } catch {
        // Stream dropped mid-read.
      }

      if (!cancelled) {
        setConnected(false);
        scheduleReconnect(attempt);
      }
    }

    void connectOnce(0);
    return () => {
      cancelled = true;
      controller.abort();
      if (timeoutId) clearTimeout(timeoutId);
    };
  }, []);

  return { events, connected };
}
