# Field Horizon — Implementation Brief IV: COMMAND INTERFACE (Phases UI-0 … UI-7)

Builds the FIELD HORIZON // COMMAND INTERFACE: one React frontend, two delivery
targets — a Tauri 2 desktop thin client AND a browser web interface served by the
existing FastAPI server at /ui on 127.0.0.1. Same code, same API, same contracts.

Before starting:
1. Save the FABLE master prompt into the repo as docs/gui/FABLE_MASTER_PROMPT.md —
   the phases below cite its sections (§) instead of duplicating them.
2. Snapshot: git add -A && git commit -m "pre-ui snapshot"
3. Paste ONE phase at a time. UI-1 ends in a STOP for your authorization, like Brief III.

Non-negotiable corrections to the FABLE doc, baked into every phase:
- Do NOT create a new FastAPI layer. EXTEND the existing Phase-10 server and
  FieldHorizonEngine facade. One application-service layer shared by CLI, API, UI.
- Do NOT invent a second event system. The UI's SSE stream is a projection of the
  Phase-A domain-event fabric (or, if Phase A is not yet merged, of the current
  event logging — state which in the audit).
- The web target is browser-based, NOT internet-exposed: 127.0.0.1 binding and
  token auth remain hardcoded. No CORS beyond localhost. No telemetry.
- Add the missing screens the FABLE doc forgot: WEATHER, SCHOOLS, COUNCILS, DREAMS.

---

## Phase UI-0 — Ground rules (paste first, every new session)

```
Read FIELD_HORIZON_REVIEW.md and docs/gui/FABLE_MASTER_PROMPT.md fully. All standing
rules from every prior brief remain in force (preserve the weirdness and naming;
data/ off-limits; closed local instrument; tests-per-task; one focused commit per task;
no scope resurrection — no PostgreSQL, no crawler, no monorepo of other Pomegranate
projects, no rewrite of the engine core for frontend convenience).

Additional rules for the Command Interface work:

1. REALITY FIRST (FABLE §3.1, §4). Audit the actual repo before designing anything.
   Never present a feature as working when it is absent or partial: hide it, mark it
   experimental, or explain availability — enforced at runtime via a /capabilities
   endpoint, not just in docs.
2. NO FAKE FACADE (FABLE §3.2). No dead buttons, no invented data, no console.log
   handlers, no fictional endpoints. Demo data exists only behind an explicit
   DEMO MODE build flag with a permanent, visually unmistakable banner.
3. ONE SERVICE LAYER. CLI, existing server routes, and new UI endpoints all call the
   same FieldHorizonEngine/application services. No business logic in routers or in
   the frontend.
4. ONE FRONTEND, TWO TARGETS. apps/desktop hosts the React/TypeScript/Vite app;
   Tauri 2 wraps it for desktop; the SAME production build is served by the existing
   FastAPI server at /ui for the browser. No forked codepaths beyond a thin
   platform-adapter (file dialogs, managed-backend controls exist only in Tauri).
5. LOCALHOST ONLY. 127.0.0.1 binding and bearer-token auth are hardcoded for both
   targets. The web UI must obtain the token via the documented local flow, never
   embed it in the bundle.
6. CONTRACT-FIRST. OpenAPI is the contract; TypeScript types are GENERATED from it;
   a CI check fails if frontend types drift from the backend schema.
7. BILINGUAL (FABLE §7): all UI strings through i18n, French and English, no
   hardcoded copy in components. Engine vocabulary (CANON, HERESY, pressure, dream)
   stays untranslated where translation would blur it.
8. GREEN GATE per task: pytest+ruff+mypy on backend; tsc --noEmit, eslint, vitest on
   frontend; playwright where E2E exists; plus a live smoke test against the real
   corpus.

Acknowledge rules 1–6 explicitly, then wait for my Phase UI-1 message. No code yet.
```

## Phase UI-1 — Reality audit (dossier, then STOP)

```
Produce the audit, then STOP for authorization. No implementation.

1. docs/gui/REPOSITORY_REALITY_AUDIT.md with the capability matrix from FABLE §4
   (feature | present | partial | documented-only | module | CLI command | data |
   exposable-in-UI). Cover at minimum: ingestion (books, JSON corpus, manifest),
   FTS + semantic retrieval, hybrid explain, retrieval planner v3 and whether
   legacy/shadow/active modes actually exist, evidence selection and scoring,
   provenance/lineage/supply-chain, contradictions (what actually detects them
   today — pressure edges? planner? nothing?), cycles/agents/synthesis/evaluation/
   critic/rewrite, canon + temporal as-of queries, councils, schools, weather axes
   and readings, dream runs and ontology proposals, domain events (exact current
   coverage: server vs CLI; Phase A status), replay levels, registries, exports
   (incl. Godot), models/Ollama management, PrincipalContext, config, diagnostics.
   Ground every row in a real file path and symbol.
2. docs/gui/UI_FEATURE_COVERAGE.md skeleton mapping each PRESENT capability to its
   planned screen, endpoint, and test — including the four screens the FABLE doc
   omitted: WEATHER, SCHOOLS, COUNCILS, DREAMS.
3. Identity audit (FABLE §5): search locally for the pomegranateinteractive.com
   repo (~/Projects, sibling dirs, git remotes). If found, extract real tokens
   (colors, fonts, spacing, motifs) into a draft docs/gui/DESIGN_SYSTEM.md. If not
   found, say so plainly and draft the fallback direction from FABLE §6. Do not
   fabricate an audit of a site you could not read.
4. Architecture note: how the existing server will serve /ui static files; how the
   SSE event stream projects the existing event fabric; the /capabilities endpoint
   design (what it reports, how the UI gates on it); the Tauri managed-backend
   ("sidecar") vs attach-to-running flow per FABLE §8.4; the token flow for the
   browser target.
5. Risks and open questions that genuinely block implementation.

STOP after presenting all of this. Implement nothing until I authorize.
```

## Phase UI-2 — Foundation: services, API extension, events, contract

```
Authorized scope, one commit per numbered item, green gate each:

1. Application-service consolidation: ensure every operation the UI needs is exposed
   on FieldHorizonEngine / an application layer (corpus, retrieval+explain, cycles,
   canon incl. as-of, lineage/supply-chain, contradictions source (whatever the audit
   found), weather, schools, councils, dreams, proposals, models, system). CLI and
   server keep calling the same layer. Refactor only where the audit showed
   duplication; no behavior changes.
2. Extend the EXISTING FastAPI app: /capabilities (engine version, schema version,
   feature flags derived from what actually exists — this drives UI gating),
   /health (deep check: db, schema, model provider, event channel), and the missing
   read endpoints identified in UI_FEATURE_COVERAGE.md. Structured error model per
   FABLE §13 (code, title, detail, remediation, correlation_id) — no raw tracebacks
   in responses.
3. Event stream: GET /events/stream (SSE) projecting the domain-event fabric with
   filters (run_id, cycle_id, event_type, since). Envelope fields per FABLE §8.3
   mapped from the existing event envelope — do not invent a parallel schema. If
   Phase A is unmerged, project the current logging and mark the delta in the audit.
4. Contract: freeze response models (pydantic, schema_version), export OpenAPI to
   apps/desktop/contract/openapi.json, generate TypeScript types from it, and add a
   CI check that regenerating produces no diff.
5. Backend tests: capabilities reflects reality (toggle a feature off in a fixture
   and see it disappear), health degrades loudly, SSE ordering and filtering,
   error-model shape, CLI/API service parity.
```

## Phase UI-3 — Shell: Tauri + web target, tokens, i18n, connection

```
One commit per item, green gate each:

1. Scaffold apps/desktop: Tauri 2 + React + TypeScript + Vite (stable versions).
   Minimal Tauri capabilities/allowlist (FABLE §14). Platform adapter module: file
   dialogs and managed-backend controls in Tauri; both absent in web build.
2. Web target: `vite build` output served by the FastAPI server at /ui (static
   mount). Document the local token flow for the browser (e.g. paste-token screen or
   local login page reading the token file path shown by the CLI) — never bundle the
   token. Add scripts/field-horizon-ui (dev: backend + frontend + logs; per FABLE
   §21) plus gui-build/gui-test.
3. Design system: tokens.css / typography.css / surfaces.css / motion.css from the
   UI-1 identity audit; FABLE §6 palette semantics (garnet identity, terminal-green
   liveness, red strictly for contradiction/error/negative-verdict; two type
   registers — literary for titles/synthesis, monospace for ids/logs/scores).
   docs/gui/DESIGN_SYSTEM.md finalized.
4. App shell per FABLE §9: top bar (backend state, db, active model, planner mode,
   active cycles, language), side nav — COMMAND, CORPUS, RETRIEVAL, CYCLES,
   SEMANTICS, CANON, WEATHER, SCHOOLS, COUNCILS, DREAMS, PROVENANCE, CONTRADICTIONS,
   EVALUATION, MODELS, SYSTEM, SETTINGS — with sections auto-hidden or marked
   experimental from /capabilities; center workspace; right inspector; bottom
   console (resizable, filterable, export) fed by the SSE stream.
5. i18n (FR/EN) wired from the start; connection manager per FABLE §8.4 (attach or
   launch managed backend, availability detection, reconnect with backoff, never
   kill a process it didn't start); startup sequence per FABLE §10.1 with real
   steps and actionable errors; global state model per FABLE §17 (loading/empty/
   error/degraded/reconnecting — no blank pages, no eternal spinners).
6. Frontend tests: shell renders against a mocked /capabilities; nav gating;
   connection-loss banner; i18n switch; token flow.
```

## Phase UI-4 — Essential workflows

```
One commit per screen, green gate each; every screen uses generated types, TanStack
Query, virtualized lists, and the FABLE §17 state model. Data is always real.

1. COMMAND dashboard (FABLE §10.2): live counts from real endpoints, recent cycles,
   recent events, pipeline strip (INTERPRETER → RETRIEVAL → AGENTS → SYNTHESIS →
   EVALUATION → CRITIC/REWRITE → CANON) reflecting actual configuration — plus
   current weather readings and active-school count (small, linked to their screens).
2. CORPUS (FABLE §10.3): sources list (search/filter/sort), source sheet (id, path,
   hash, chunks, status, provenance), chunk viewer with exact offsets, manifest
   view of sources.yaml (read-only representation; edits keep going through the
   manifest file — curation as code stays authoritative), ingestion progress via
   SSE for ingest runs, errors surfaced.
3. RETRIEVAL lab (FABLE §10.4): query box, weights/thresholds/diversity controls
   bound to the real config surface, results with full --explain decomposition per
   candidate (every score component + exclusion reasons), preset save/compare.
4. CYCLES launcher (§10.6) exposing only parameters the engine really accepts;
   LIVE CYCLE (§10.7): SSE timeline, retrieval panel, agent panels (the four
   adversaries visible, arguing), synthesis, evaluation scores and verdict,
   critic/rewrite passes, canon outcome, runtime stats. No Pause button unless the
   backend truly suspends.
5. Result + PROVENANCE explorer (§10.10): from any fragment walk up and down —
   synthesis → decisions → evidence → chunks → sources → exact character offsets;
   supply-chain report view (deterministic vs LLM steps, weakest link, synthetic-
   dependency ratio, completeness score) rendered and exportable.
6. Playwright E2E: start app → connect → browse corpus → run a real retrieval →
   inspect evidence → launch a real cycle → watch events → open result → walk
   provenance → export. Runs against the real local backend and corpus.
```

## Phase UI-5 — The engine's own screens + advanced

```
One commit per screen, green gate each:

1. WEATHER observatory: current axis readings with sparklines, surface vs deep
   weather, historical timeline, cycle-to-cycle comparison, school overlays.
   Charts sober (ECharts), garnet/ivory palette, no decoration without data.
2. SCHOOLS explorer: clusters with names/summaries/sizes, membership, lineage
   across clustering runs, representative fragments, inter-school distances.
3. COUNCILS: council history, examined/overturned counts, rehabilitations and
   retirements with links into canon events and provenance.
4. DREAMS: dream-run ledger (budgets, regions explored, candidates, outcomes),
   emitted ontology proposals with evidence, and proposal review UI calling the
   real proposals apply/reject flow — apply requires explicit confirmation and
   shows the parity-check result. Never auto-apply.
5. CANON observatory (FABLE §10.9): active canon, genealogy graph (React Flow),
   promotion/retirement/rehabilitation history, temporal as-of queries with a
   timeline scrubber, version compare.
6. SEMANTICS graph (§10.8): concept/entity/motif neighborhood exploration with
   server-side aggregation, progressive expansion, density warnings — never dump
   the whole graph.
7. CONTRADICTIONS (§10.11): backed by whatever the audit found real (pressure
   edges / planner constraint output); typology per §10.11, side-by-side fragments,
   links to lineage and canon. If detection is partial, the screen says so.
8. PLANNER (§10.5): only the modes that truly exist. If shadow mode exists,
   build the synchronized legacy-vs-planner comparison; mode switches are explicit,
   confirmed, and event-logged.
9. EVALUATION lab (§10.12), MODELS (§10.13, registry-backed, no secrets in UI or
   logs), SYSTEM diagnostics (§10.14, non-destructive checks only), SETTINGS
   (§10.15, never silently overwrite config).
10. EMERGENCE MONITOR (§19): implement ONLY if the audit found real measurable
    signals (motif-frequency spikes, pressure anomalies, weather discontinuities);
    otherwise write docs/gui/EMERGENCE_MONITOR_SPEC.md and ship nothing. Detection
    criteria documented; no button manufactures an event; unsettling aesthetic,
    rigorous logic.
```

## Phase UI-6 — Hardening

```
1. Security per FABLE §14 + our standing stance: loopback binding asserted by test
   for BOTH targets; CORS localhost-only; Tauri allowlist minimal; path validation
   on any file selection; secrets never logged; destructive actions confirmed and
   event-logged. docs/gui/SECURITY_MODEL.md with the threat model (single user,
   local machine, honest about what is NOT defended).
2. Performance per FABLE §15: server pagination everywhere, virtualization,
   cancellable queries, debounce, graph aggregation; verify smooth with thousands
   of sources / tens of thousands of chunks (generate a synthetic large DB for the
   test, clearly marked, never mixed with the real one).
3. Accessibility per FABLE §16: keyboard nav, visible focus, contrast,
   prefers-reduced-motion, non-color status encodings.
4. Resilience E2E: backend killed mid-cycle (UI degrades, reconnects, resumes
   stream), db locked, model absent, empty results, long timelines, cancelled
   requests.
5. Command palette (§11) and global search (§12) — search calls existing backend
   search endpoints, it is not a second retrieval engine.
6. Exports (§18): JSON/Markdown/CSV/SVG/PNG per content type + the reproducible
   cycle bundle (config, query, ids, model, evidence, scores, events, result,
   evaluation, provenance, versions) with the Pomegranate attribution block.
7. Packaging (§22): Linux first — AppImage and .deb via Tauri bundler; document
   exactly what each artifact contains; backend as managed sidecar or external;
   no models bundled. Windows next if toolchain permits; document if not.
```

## Phase UI-7 — Delivery

```
1. Documentation set per FABLE §29 (ARCHITECTURE, API_CONTRACT, DESIGN_SYSTEM,
   UI_FEATURE_COVERAGE finalized, SECURITY_MODEL, TEST_STRATEGY, PACKAGING,
   RUNBOOK with exact commands, TROUBLESHOOTING).
2. Final report per FABLE §30: what was really built, coverage vs audit, screenshots
   of every main screen, test results, artifacts, honest limitations, prioritized
   next steps. Do not write "production ready" unless FABLE §25's acceptance
   criteria are all met — walk the checklist explicitly, item by item, and mark
   any miss.
3. Branch stays feature/field-horizon-command-interface; commit log per FABLE §28;
   no merge to main without explicit instruction.
```

---

## Running notes

Phase UI-1 is the only STOP-gated phase; read its audit as carefully as you read the
Brief-III dossiers — the whole no-fake-facade guarantee rests on the capability
matrix being true. Expect UI-2/UI-3 to be quick, UI-4/UI-5 to be the long middle,
and resist expanding UI-5 mid-flight: a screen not backed by a real capability goes
to the spec pile, not the codebase. The Godot client later consumes the same
/capabilities-gated, schema-versioned API this work freezes — which is the whole
point of one service layer.
