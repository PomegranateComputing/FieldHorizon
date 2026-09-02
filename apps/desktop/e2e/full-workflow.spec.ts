import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

/**
 * Phase UI-4 item 6: "start app -> connect -> browse corpus -> run a real
 * retrieval -> inspect evidence -> launch a real cycle -> watch events ->
 * open result -> walk provenance -> export. Runs against the real local
 * backend and corpus." No mocks, no fixtures: every assertion below reads
 * whatever this machine's `field-horizon serve` and ingested corpus
 * actually contain right now. See README.md's "E2E tests" section for
 * prerequisites (a running backend with a real token file).
 */

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");
const TOKEN_FILE = process.env.FH_TOKEN_FILE ?? path.join(REPO_ROOT, ".fh_token");

function readToken(): string {
  if (!existsSync(TOKEN_FILE)) {
    throw new Error(
      `No token file at ${TOKEN_FILE}. Start the real backend first: ` +
        `field-horizon serve --token-file ${TOKEN_FILE} (see README.md's "E2E tests" section).`,
    );
  }
  return readFileSync(TOKEN_FILE, "utf-8").trim();
}

test.describe("full workflow against the real backend", () => {
  test.beforeEach(async ({ page }) => {
    const token = readToken();
    await page.goto("/");
    await page.evaluate((t) => window.sessionStorage.setItem("field-horizon-api-token", t), token);
    await page.reload();
    await expect(page.locator(".app-shell")).toBeVisible({ timeout: 15_000 });
  });

  test("connect, browse corpus, retrieve, launch a cycle, walk provenance, export", async ({ page }) => {
    // -- browse corpus --------------------------------------------------
    await page.getByRole("link", { name: "CORPUS", exact: true }).click();
    await expect(page.getByRole("heading", { name: "CORPUS" })).toBeVisible();

    const sourceRows = page.locator(".corpus-source-row");
    await expect(sourceRows.first()).toBeVisible({ timeout: 10_000 });
    const sourceCount = await sourceRows.count();
    expect(sourceCount).toBeGreaterThan(0);

    // inspect evidence: selecting a source loads its real chunk detail
    // (exact character offsets) into the right-hand Inspector panel.
    await sourceRows.first().click();
    await expect(page.locator(".inspector-fields")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(".inspector-chunk-list li").first()).toBeVisible({ timeout: 10_000 });

    // -- run a real retrieval --------------------------------------------
    await page.getByRole("link", { name: "RETRIEVAL", exact: true }).click();
    await expect(page.getByRole("heading", { name: "RETRIEVAL" })).toBeVisible();

    const retrievalInput = page.locator(".retrieval-query-input");
    await expect(retrievalInput).toBeEnabled({ timeout: 10_000 });
    await retrievalInput.fill("faith");
    await page.locator(".retrieval-form button[type=submit]").click();

    const candidates = page.locator(".retrieval-candidate");
    await expect(candidates.first()).toBeVisible({ timeout: 15_000 });
    // Full --explain decomposition: every score component + its contribution.
    await expect(page.locator(".retrieval-components").first()).toBeVisible();

    // -- launch a real cycle and watch it live ---------------------------
    await page.getByRole("link", { name: "CYCLES", exact: true }).click();
    await expect(page.getByRole("heading", { name: "CYCLES" })).toBeVisible();

    // Dry run is the form's default, but it skips both real agent debate and
    // real synthesis (fieldhorizon/multicycle.py's dry_run branch) -- this
    // test unchecks it so the four adversaries genuinely run once through a
    // real browser end to end, the single most complex live-UI feature this
    // phase built. Auto-rewrite stays on: it's the form's own default and
    // only adds two more calls when evaluation actually triggers it.
    const cycleQuery = `e2e smoke test ${Date.now()}`;
    await page.locator(".cycles-query-input").fill(cycleQuery);
    await page.locator(".cycles-checkbox", { hasText: /Dry run|Essai à blanc/ }).locator("input").uncheck();
    await page.locator(".cycles-form button[type=submit]").click();

    // SSE timeline: real domain events streaming in as the cycle runs
    // (interpretation -> retrieval -> four agents -> synthesis -> evaluation).
    await expect(page.locator(".cycles-live")).toBeVisible({ timeout: 15_000 });
    await expect(page.locator(".cycles-timeline li").first()).toBeVisible({ timeout: 15_000 });

    // The four adversaries, genuinely arguing (FABLE Sec.10.7).
    await expect(page.locator(".cycles-agent-card")).toHaveCount(4, { timeout: 2 * 60 * 1000 });

    // -- open result -------------------------------------------------------
    const successLink = page.locator(".cycles-success a");
    await expect(successLink).toBeVisible({ timeout: 5 * 60 * 1000 });
    const cycleId = (await page.locator(".cycles-success").textContent())?.match(/(\d+)/)?.[1];
    expect(cycleId).toBeTruthy();
    await successLink.click();

    // -- walk provenance -----------------------------------------------
    await expect(page.getByRole("heading", { name: "PROVENANCE" })).toBeVisible();
    await expect(page.locator(".provenance-form input")).toHaveValue(cycleId!);
    await expect(page.locator(".provenance-lineage-tree")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(".provenance-fields")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(".provenance-fields")).toContainText(cycleId!);

    // -- export ------------------------------------------------------------
    const downloadPromise = page.waitForEvent("download");
    // Scoped to .provenance-form: the CONSOLE panel has its own "Export"
    // button (event log export), an unrelated match for the same word.
    await page.locator(".provenance-form").getByRole("button", { name: /Export|Exporter/ }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toContain(cycleId!);
  });
});
