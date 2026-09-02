/// <reference types="vitest/config" />
import { defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";
import { configDefaults } from "vitest/config";

// @ts-expect-error process is a nodejs global
const host = process.env.TAURI_DEV_HOST;

// The frontend's own API client (src/api/client.ts) always uses relative
// paths -- same-origin once served at /ui in production. In `npm run dev`
// the frontend runs on its own Vite port (1420), separate from the backend
// (field-horizon serve, default port 8777), so these routes are proxied
// there instead: the client code never needs to know which mode it's in.
const BACKEND_DEV_TARGET = "http://127.0.0.1:8777";
const API_ROUTES = [
  "/status",
  "/system-info",
  "/config",
  "/canon",
  "/cycles",
  "/why-changed",
  "/lineage",
  "/provenance",
  "/weather",
  "/schools",
  "/councils",
  "/sources",
  "/fingerprint",
  "/export",
  "/retrieve",
  "/cycle",
  "/multi-cycle",
  "/metrics",
  "/capabilities",
  "/health",
  "/events",
  "/registry",
  "/proposals",
  "/dreams",
  "/semantics",
  "/planner",
  "/evaluation",
  "/ingest",
  "/manifest",
];
const apiProxy: Record<string, ProxyOptions> = Object.fromEntries(
  API_ROUTES.map((route) => [route, { target: BACKEND_DEV_TARGET, changeOrigin: true }]),
);

// https://vite.dev/config/
export default defineConfig(async () => ({
  plugins: [react()],

  // Vite options tailored for Tauri development and only applied in `tauri dev` or `tauri build`
  //
  // 1. prevent Vite from obscuring rust errors
  clearScreen: false,
  // 2. tauri expects a fixed port, fail if that port is not available
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      // 3. tell Vite to ignore watching `src-tauri`
      ignored: ["**/src-tauri/**"],
    },
    proxy: apiProxy,
  },

  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // e2e/ holds Playwright specs (a separate test runner, real backend
    // required) -- excluded here so `npm test` (vitest) never tries to
    // execute them as unit tests.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
}));
