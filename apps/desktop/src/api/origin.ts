import { isTauri } from "../platform";

/**
 * Where API requests go.
 *
 * Browser build: relative URLs. Served at /ui by `field-horizon serve`
 * itself (same origin), or proxied to it by Vite during `npm run dev`.
 *
 * Tauri webview: the page's own origin is the webview's asset protocol --
 * http://tauri.localhost on Windows (WebView2), tauri://localhost elsewhere
 * -- not the backend. Relative requests went there, and on Windows the
 * asset handler answered GET /health with index.html, so the app never
 * left its startup screen. In the webview the backend must be addressed
 * explicitly. `field-horizon serve` only ever binds 127.0.0.1
 * (fieldhorizon/server.py HOST), so the host is fixed; only the port is a
 * setting, defaulting to the port the managed launch uses.
 */
export const DEFAULT_BACKEND_PORT = 8777;
export const BACKEND_PORT_STORAGE_KEY = "field-horizon-backend-port";
const LOOPBACK_HOST = "127.0.0.1";

function parsePort(value: string | null): number | null {
  if (value === null || !/^\d{1,5}$/.test(value)) return null;
  const port = Number(value);
  return port >= 1 && port <= 65535 ? port : null;
}

export function backendPort(): number {
  return parsePort(window.localStorage.getItem(BACKEND_PORT_STORAGE_KEY)) ?? DEFAULT_BACKEND_PORT;
}

export function setBackendPort(port: number): void {
  const parsed = parsePort(String(port));
  if (parsed === null) throw new Error(`Invalid backend port: ${port}`);
  window.localStorage.setItem(BACKEND_PORT_STORAGE_KEY, String(parsed));
}

export function apiBaseUrl(): string {
  return isTauri() ? `http://${LOOPBACK_HOST}:${backendPort()}` : "";
}
