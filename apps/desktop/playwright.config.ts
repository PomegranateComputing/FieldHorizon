import { defineConfig } from "@playwright/test";

/**
 * Phase UI-4 item 6. This suite is NOT hermetic: it drives the real
 * FastAPI backend (fieldhorizon/server.py) against whatever local corpus
 * is already ingested, and one test genuinely launches a multi-agent
 * cycle through the real model provider -- there is no mock/fixture mode.
 * `field-horizon serve` must already be running (see README.md's "E2E
 * tests" section for the exact prerequisites); this config only manages
 * the frontend dev server, since that part is stateless and safe to
 * start/reuse automatically.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 5 * 60 * 1000,
  expect: { timeout: 15 * 1000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://localhost:1420",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:1420",
    reuseExistingServer: true,
    timeout: 30 * 1000,
  },
});
