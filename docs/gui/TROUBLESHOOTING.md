# Troubleshooting (Phase UI-7)

Real symptoms this project actually hit, and their real fixes — not a
generic checklist.

## "Works via `curl`, 404s through the frontend dev server"

`vite.config.ts`'s `API_ROUTES` array is the dev-proxy allowlist — every
new backend route prefix must be added there, or `npm run dev` won't
forward it to the real backend at all. This was hit and fixed **five
separate times** across this project (`/ingest`+`/manifest`, `/dreams`,
`/semantics`, `/planner`, `/evaluation`) before the lesson fully stuck.
After adding a route, **restart** the Vite dev server — the proxy config
is only read at startup, a browser reload isn't enough.

## "database is locked"

Two independent layers now guard against this (Phase UI-6 item 4):
`fieldhorizon/db.py`'s `connect()` sets `PRAGMA busy_timeout = 5000`, so a
second connection hitting a writer mid-transaction waits instead of
failing instantly; if it still happens, `fieldhorizon/server.py` returns
a specific `FH_DATABASE_BUSY` 503 ("Another operation ... is writing to
the database. Retry in a moment.") rather than a generic 500. If you see
this repeatedly rather than as a rare transient blip, something is
holding a write transaction open far longer than expected — check for a
long-running CLI operation (ingest, dream run) against the same database
file.

## Model shows "degraded" on the SYSTEM screen

Two different messages, two different fixes:

* `"Ollama unreachable: ..."` — the Ollama process isn't running, or
  `config.yaml`'s `ollama.base_url` is wrong.
* `"... is not pulled -- run \`ollama pull <model>\`"` — Ollama is up and
  reachable, but the model named in `config.yaml`'s `ollama.default_model`
  was never actually pulled. Run the suggested command.

## Backend killed / crashed mid-session

The UI is designed to survive this (`docs/gui/RESILIENCE_MODEL.md`): the
top bar flips to `FAILED`, a banner shows "reconnecting, attempt N," and
it retries with backoff (1s, 2s, 4s, 8s, 16s, 30s, capped) for up to 8
attempts (~2 minutes total) before giving up and showing a manual retry
button. If the backend comes back up within that window it reconnects on
its own — no reload needed. If it's been down longer, use the manual
retry button once it's back.

## AppImage won't run: "Cannot mount AppImage, please check your FUSE setup"

Common in containers/sandboxes with no FUSE available. Run it with
`--appimage-extract-and-run` instead of a bare invocation — this is how
it was actually verified in this project's own build environment (see
`docs/gui/PACKAGING_MODEL.md`).

## A React Flow node renders outside its panel / intercepts clicks meant for something else

Hit on both CANON's genealogy graph and SEMANTICS' neighbor graph during
development. The graph's wrapper `<div>` needs `overflow: hidden` and
`position: relative` — without both, a node can render past the intended
height and overlap a sibling panel. Already fixed in both screens'
CSS; if a new React Flow screen is added later, apply the same two
properties to its wrapper from the start.

## Writing a new Playwright verification script: French renders by default

FABLE §7 makes French the default UI language, and it stayed that way
across dev-server restarts within a session in a couple of cases this
project hit directly. A `getByRole("button", { name: /export/i })` or
`getByPlaceholder(/cycle id/i)` written assuming English text will
silently fail to match French copy ("Exporter," "Id de cycle..."). Either
click the "EN" language button first in the script, or write
language-agnostic locators (test ids, CSS classes) for anything that must
work regardless of active language.

## A throwaway `npm install -D playwright` leaves an unrelated `package-lock.json` diff

This repo already has a real, permanent Playwright dependency for the E2E
suite (`@playwright/test`). Installing the standalone `playwright` package
temporarily for a manual verification pass, then uninstalling it, can
still leave `package-lock.json` changed — npm's dependency deduplication
shifts `@playwright/test`'s own nested `playwright`/`playwright-core`
resolution in the process. Always check
`git status apps/desktop/package-lock.json` after this pattern; if the
diff is only that dedup shift (not an intentional dependency change),
`git checkout -- apps/desktop/package-lock.json` and re-run `npm ci` to
confirm the restored lockfile still installs cleanly.

## Debugging the frontend itself

Browser build: normal devtools (network tab shows every `ApiError`'s real
response body — `code`/`detail`/`remediation` are never hidden from the
network inspector even when the UI only shows a summary). Tauri build:
most Tauri dev builds expose the same webview devtools via a keyboard
shortcut or right-click "Inspect".

## Something not listed here

Check `GET /health` first (SYSTEM screen or directly) — it reports
`db`/`schema`/`model_provider`/`event_channel` individually, each with a
real detail string. Then check the bottom console / `GET /events` for the
actual domain-event history of whatever operation is misbehaving — this
app never hides its own event log.
