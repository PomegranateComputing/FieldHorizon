import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiFetch, apiUrl } from "./client";
import { BACKEND_PORT_STORAGE_KEY, apiBaseUrl, backendPort, setBackendPort } from "./origin";

function enterTauri() {
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
}

describe("API origin", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    vi.unstubAllGlobals();
  });

  it("stays relative in the browser build (same origin at /ui, Vite proxy in dev)", () => {
    expect(apiBaseUrl()).toBe("");
    expect(apiUrl("/health")).toBe("/health");
  });

  it("addresses the loopback backend from the Tauri webview, never tauri.localhost", () => {
    enterTauri();
    expect(apiUrl("/health")).toBe("http://127.0.0.1:8777/health");
  });

  it("honours a stored port and ignores an invalid one", () => {
    enterTauri();
    setBackendPort(9123);
    expect(apiBaseUrl()).toBe("http://127.0.0.1:9123");

    for (const bad of ["0", "65536", "80; rm", "-1", "", "8777.5"]) {
      window.localStorage.setItem(BACKEND_PORT_STORAGE_KEY, bad);
      expect(backendPort()).toBe(8777);
    }
    expect(() => setBackendPort(70000)).toThrow("Invalid backend port");
  });

  it("sends the bearer token to the loopback backend in Tauri", async () => {
    enterTauri();
    window.sessionStorage.setItem("field-horizon-api-token", "tok");
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/health");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8777/health");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok");
  });
});
