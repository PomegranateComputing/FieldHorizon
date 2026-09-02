# Final Report — Field Horizon // Command Interface (Phase UI-7)

Implementation Brief IV, branch `feature/field-horizon-command-interface`.
Per FABLE §30.

## Résumé

A real, working command interface for Field Horizon exists now, on two
delivery targets built from one codebase: a Tauri 2 desktop app and a
browser page served by the existing FastAPI backend at `/ui`. Seventeen
screens, a command palette and global search, real exports including a
reproducible cycle bundle, and a hardening pass covering security,
performance, accessibility, resilience, and Linux packaging. Nothing here
is a skin over a terminal or a chatbot — every screen calls the same
engine facade and event fabric the CLI already used, extended, not
duplicated.

## Audit

`docs/gui/REPOSITORY_REALITY_AUDIT.md` (Phase UI-1) is the original
capability inventory this project was built against — read that for the
full initial finding. In short: Field Horizon already had a mature,
tested backend (retrieval, multi-agent synthesis, canon/genealogy,
councils, provenance, dreams/ontology proposals, registries, schools,
weather) with no GUI at all. The audit's job was distinguishing what was
real and just needed a route, from what didn't exist and would need to be
built honestly (or explicitly deferred) rather than faked.

## Couverture

`docs/gui/UI_FEATURE_COVERAGE.md` is the finalized, as-built map — every
screen, the real backend capability it exposes, the exact route(s), and
how it was verified. Not reproduced here in full; see that file.

## Architecture

`docs/gui/ARCHITECTURE.md` has the full picture. The decisions that
mattered most:

* **No second engine, no second event system** — every route in
  `fieldhorizon/server.py` calls the same `FieldHorizonEngine` facade and
  `domain_events` fabric the CLI already used.
* **Contract-first API** — `fieldhorizon/contracts.py` → generated
  OpenAPI + TypeScript types, with a test that fails if they drift.
* **One frontend, two delivery targets** — a `PlatformAdapter` seam
  (`apps/desktop/src/platform/`) is the only place the React tree
  branches on Tauri vs. browser.
* **External, not sidecar, backend** — the desktop app manages a real
  user-owned Python process; it never bundles Python, models, or the
  `fieldhorizon` package. Documented, not silently reinterpreted, in
  `docs/gui/PACKAGING.md`/`PACKAGING_MODEL.md`.
* **Audit-first hardening** — every Phase UI-6 item started with a
  background-agent audit of what was actually real before touching
  anything, closing only genuine gaps rather than rebuilding what already
  worked.

## Fichiers

Not an exhaustive listing (see `git log feature/field-horizon-command-interface`
for that) — the load-bearing ones:

* `fieldhorizon/server.py`, `fieldhorizon/contracts.py` — the whole API surface.
* `fieldhorizon/engine.py` — every new facade method this project added.
* `apps/desktop/src/` — the entire frontend; `pages/` (one file per
  screen), `shell/` (app chrome, command palette, global search, event
  stream/console/inspector contexts), `connection/` (startup + reconnect
  state machine), `platform/` (Tauri/web adapter), `api/` (typed client),
  `charts/` (ECharts wrapper), `styles/` (design tokens).
* `apps/desktop/src-tauri/src/backend.rs` — the managed-backend process
  spawn, argument-validated, unit-tested.
* `contract/` — generated OpenAPI schema + TypeScript types.
* `docs/gui/` — this entire documentation set, plus one `*_MODEL.md` per
  Phase UI-6 hardening item recording what was audited, fixed, and
  verified.

## Commandes

Full detail in `docs/gui/RUNBOOK.md`. The essentials:

```bash
./scripts/field-horizon-ui              # dev, both halves, one command
cd apps/desktop && npm run build        # frontend build
npx tauri build                          # Linux .deb + AppImage
.venv/bin/python -m pytest -q            # backend tests
cd apps/desktop && npx vitest run        # frontend tests
cd apps/desktop/src-tauri && cargo test  # Rust tests
```

## Tests

| Suite | Result |
|---|---|
| Backend (pytest) | **635 passed** |
| Backend lint/types (ruff, mypy) | clean, 52 source files |
| Rust (cargo test) | **6 passed** |
| Frontend types (tsc) | clean |
| Frontend (vitest) | **46 passed**, 13 files |
| Frontend build (vite) | clean |
| OpenAPI contract drift test | passes (contract matches live schema) |
| E2E (Playwright, real backend) | exists (`e2e/full-workflow.spec.ts`), not run as part of this report's gate — see `docs/gui/TEST_STRATEGY.md` for why it's intentionally excluded from CI |

Full methodology, including the live-Playwright-verification discipline
used for every screen and hardening item (never faked, always cleaned up
after), in `docs/gui/TEST_STRATEGY.md`.

## Packaging

Real Linux artifacts, built and inspected this project (not this report's
session alone — Phase UI-6 item 7):

* `Field Horizon_0.1.0_amd64.deb` (~3.4 MB)
* `Field Horizon_0.1.0_amd64.AppImage` (~83 MB)

Both inspected directly (`dpkg-deb -c/-I`, walking the AppImage's AppDir),
confirmed to contain no Python/models/backend code, and the AppImage was
actually launched (via `--appimage-extract-and-run` under `xvfb-run`,
since this environment has no FUSE) and stayed up without crashing.
Windows: not attempted, no toolchain available, documented in
`docs/gui/PACKAGING_MODEL.md`.

## Captures

`docs/gui/screenshots/` — one real screenshot per screen (COMMAND,
CORPUS, RETRIEVAL, PLANNER, CYCLES, SEMANTICS, CANON, WEATHER, SCHOOLS,
COUNCILS, DREAMS, PROVENANCE, CONTRADICTIONS, EVALUATION, MODELS, SYSTEM,
SETTINGS), taken against the real development database, English UI, this
session.

## Acceptance criteria (FABLE §25), walked explicitly

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | The existing CLI still works | **Met** | Backend engine facade unchanged in its CLI-facing behavior; CLI-adjacent commands (`replay`, `registry`, `provenance`, `dream`, `proposals`, `council`, etc.) are exactly what the new routes call into, and all 635 backend tests (which exercise these same code paths) pass. |
| 2 | The client starts with a documented command | **Met** | `./scripts/field-horizon-ui` (RUNBOOK.md), verified this session. |
| 3 | The local backend is detected or launched cleanly | **Met** | StartupScreen + `ConnectionContext.launchManagedBackend`; Rust-side argument validation (`validate_repo_root`/`validate_python_bin`) is unit-tested. |
| 4 | System state is visible | **Met** | SYSTEM screen + TopBar, both showing live `/health`/`/system-info`. |
| 5 | Real sources can be browsed | **Met** | CORPUS, verified against both the real dev DB and a synthetic 3,000-source DB. |
| 6 | A real search can be executed | **Met** | RETRIEVAL (`/retrieve`) and Global Search, verified against the real corpus. |
| 7 | A real cycle can be launched | **Met** | CYCLES; the E2E suite genuinely launches one non-dry-run multi-agent cycle. |
| 8 | Events are visible live | **Met** | SSE-backed console and LIVE CYCLE timeline; resume-with-no-gap verified in `RESILIENCE_MODEL.md`. |
| 9 | The result can be inspected | **Met** | CYCLES/CANON result views, Inspector panel. |
| 10 | Evidence can be opened | **Met** | RETRIEVAL's score-component explain, the reproducible cycle bundle's evidence section (real `cycle_sources` rows). |
| 11 | Provenance can be followed | **Met** | PROVENANCE screen (lineage + supply-chain report), verified against a real cycle. |
| 12 | Errors are understandable | **Met** | Uniform `ErrorResponse` contract (code/detail/remediation), verified live for 404/locked-DB/model-absent cases. |
| 13 | Data is not simulated | **Met** | Standing discipline of this whole project — every screen verified against a real backend; scratch installs used only for genuinely destructive/synthetic-scale tests, always disposable and clearly labeled, never shipped in application code. |
| 14 | French works | **Met** | Default language; verified every screen this session and throughout. |
| 15 | English works | **Met** | Verified every screen this session and throughout. |
| 16 | The application is usable via keyboard | **Met** | Global `:focus-visible` ring, command palette full keyboard operability (type/arrows/enter/escape), React Flow's own keyboard defaults confirmed via source inspection, DreamsPage dialog focus trap. |
| 17 | The interface stays readable at 1366×768 | **Met** | Verified live this session (COMMAND screenshot, no clipping/overlap). |
| 18 | The interface properly uses a 2560×1440 screen | **Met** | Verified live this session — found and fixed a real gap (WEATHER's chart panels were capped at `max-width: 900px`, leaving most of the screen empty; raised to 1400px and re-verified). Other screens' narrower content widths are a deliberate readability choice (bounded prose/list width), not a bug — spot-checked CONTRADICTIONS and CANON to confirm they read as intentional, not broken. |
| 19 | Linux packaging works | **Met** | Real `.deb`/`.AppImage` built, inspected, and the AppImage actually launched. |
| 20 | The essential tests pass | **Met** | 635 backend + 46 frontend + 6 Rust, all green; see Tests above. |
| 21 | The documentation lets someone else launch the project | **Met** | `docs/gui/RUNBOOK.md`. |
| 22 | Absent functions are honestly identified | **Met** | `UI_FEATURE_COVERAGE.md`'s "Explicitly not covered" section, each `*_MODEL.md`'s own gaps, `EMERGENCE_MONITOR_SPEC.md` (not built — no real signal to visualize). |
| 23 | No secret is committed | **Met** | Verified this session: no `.env`/token/key files tracked, `.fh_token` gitignored, `config.yaml` carries no secrets (audited Phase UI-1). |
| 24 | No external telemetry is introduced | **Met** | The only outbound call anywhere in the server is the local Ollama health probe (`SECURITY_MODEL.md`). |

**All 24 criteria are met against the verification actually performed.**
That verification is real and specific (see the Evidence column and each
referenced doc), but it is this project's own testing — not independent
QA, not a security audit by a third party, and not exhaustive manual
testing of every control on every screen. Treat "met" as "verified by the
checks described," not as a guarantee beyond what those checks cover.

## Limites

* **EMERGENCE MONITOR** was not built — no real Basilisk/emergence/
  anomaly/recursion/attractor detection logic exists in the repository to
  visualize (FABLE §19's own conditional). `EMERGENCE_MONITOR_SPEC.md`
  documents detection criteria for if that logic is ever added.
* **Global search** doesn't cover semantic profiles or settings/params —
  no searchable data model exists for either.
* **SVG/PNG export** for React Flow graphs (CANON, SEMANTICS) wasn't
  built — would need a new dependency and non-trivial layout capture;
  ECharts' native export covered WEATHER instead.
* **The reproducible cycle bundle's evaluation section** is a live
  recomputation, not the cycle's original per-component evaluation
  breakdown — that breakdown was never persisted anywhere by the engine
  itself. Labeled `recomputed_now: true`, not silently presented as
  original.
* **Its contradictions section** is the current corpus-wide snapshot, not
  scoped to the specific cycle — no cycle/axiom linkage exists for
  contradictions anywhere in the backend. Labeled
  `scoped_to_this_cycle: false`.
* **Windows packaging** was not attempted — no toolchain in this
  environment.
* **Registry/proposals/dream-runs/schools-all list endpoints** remain
  unbounded (no pagination) — deliberately, since those tables are small
  by construction in realistic use; documented in `PERFORMANCE_MODEL.md`
  rather than pagination added speculatively.
* **Locked-DB recovery** (the `busy_timeout`/`FH_DATABASE_BUSY` fix) is
  unit-tested but was not exercised live under genuine concurrent write
  contention.
* **Same-session backend-restart recovery** (as opposed to a fresh page
  reload) was proven deterministically by a unit test, not observed live
  to completion — the disposable dev-server processes used for that live
  attempt were killed by the sandbox environment itself twice mid
  observation, unrelated to the app; see `RESILIENCE_MODEL.md`.
* No independent security review, load testing at real-world scale beyond
  the one synthetic benchmark, or cross-browser testing (only Chromium,
  via Playwright, was used for every live verification pass) has been
  performed.

## Prochaines étapes, by priority

1. **If Windows support matters**: get a Windows or cross-compilation-
   capable environment and run `docs/gui/PACKAGING.md`'s build steps
   there; document the result the same way Linux was documented.
2. **If the reproducible cycle bundle needs to reflect original
   evaluation scores**: persist the per-component `CycleEvaluation`
   breakdown at cycle time (a real engine/schema change, not a GUI one) —
   currently only the rolled-up verdict/score survive.
3. **If contradictions need to be cycle-scoped**: add real cycle/axiom
   linkage to contradiction detection in the engine — currently there is
   none to expose.
4. **If EMERGENCE MONITOR becomes relevant**: build the real detection
   signals `EMERGENCE_MONITOR_SPEC.md` already specifies, then the screen
   itself is a comparatively small follow-on.
5. **Locked-DB and same-session-restart live verification**: worth a
   deliberate re-attempt outside this session's sandbox constraints
   (e.g., a real concurrent-write test harness, a more stable
   long-running dev-server environment) to close the two live-verification
   gaps noted above — both are already unit-tested, this would only
   strengthen confidence further.
6. **React Flow SVG/PNG export**, if genuinely wanted: evaluate
   `html-to-image` (or equivalent) as a new, justified dependency rather
   than working around its absence.

Do not describe this project as "production ready" beyond what the
acceptance-criteria table above actually supports — it is a real,
working, tested, honestly-scoped instrument, verified the ways this
report describes and no further.
