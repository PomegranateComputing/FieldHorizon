# Architecture (Phase UI-7)

How Field Horizon's Command Interface is actually put together, as built —
not the plan from `docs/gui/REPOSITORY_REALITY_AUDIT.md`/`IMPLEMENTATION_BRIEF_IV.md`,
the result of building it.

## The one non-negotiable: no second engine

FABLE's and the user's own standing instruction was explicit: do not create
a second FastAPI layer or a second event system. Every route in
`fieldhorizon/server.py` is a thin wrapper over the exact same
`FieldHorizonEngine` facade (`fieldhorizon/engine.py`) the CLI
(`fieldhorizon/cli.py`) already calls, and every domain event this GUI
displays comes from the exact same `domain_events` table and
`OperationEmitter` fabric (`fieldhorizon/events.py`) Implementation Brief
III built. The CLI still works unmodified — confirmed throughout by the
backend test suite exercising both surfaces against the same engine
methods.

## Two delivery targets, one frontend

`apps/desktop/` is a single React 19 + TypeScript + Vite 7 codebase built
once (`npm run build` → `apps/desktop/dist`) and served two ways:

1. **Tauri 2 desktop app** (`apps/desktop/src-tauri/`) — a native window
   loading the built assets directly, with a small Rust layer
   (`src-tauri/src/backend.rs`) that can spawn and manage a real local
   Python process running `field-horizon serve`.
2. **Browser, via the existing FastAPI server** — `fieldhorizon/server.py`
   mounts the same `dist/` output as static files at `/ui`
   (`UI_DIST_DIR`), so the identical build also runs as a normal web page
   against a backend the user starts themselves.

`apps/desktop/src/platform/` is the seam between them: a `PlatformAdapter`
interface (`platform/types.ts`) with a Tauri implementation
(`platform/tauri.ts` — real process spawn/stop, native file dialogs,
native confirm) and a web implementation (`platform/web.ts` — `backend:
null`, `window.confirm`, dialogs that throw a clear "not available in the
browser build" error rather than silently no-op). `getPlatform()`
(`platform/index.ts`) resolves which one to use at runtime by checking for
the Tauri global — the rest of the React tree never branches on delivery
target directly.

## Backend surface

* **Contract-first API** — `fieldhorizon/contracts.py` (Pydantic models)
  is the single source of truth. `python -m fieldhorizon.openapi_export`
  regenerates `contract/openapi.json`; `cd contract && npm run generate`
  (wrapping `openapi-typescript`) regenerates `contract/types.gen.ts`,
  which `apps/desktop/src/api/types.ts` re-exports the specific types
  each screen needs from. `tests/test_openapi_contract.py` fails if the
  committed `openapi.json` ever drifts from what the live app actually
  serves — the contract can't silently go stale.
* **Auth** — one bearer token per running backend instance
  (`load_or_create_token`, `chmod 600`), checked via
  `secrets.compare_digest` on every route (`require_token` dependency).
  See `docs/gui/TOKEN_FLOW.md` and `docs/gui/SECURITY_MODEL.md`.
* **Network exposure** — `HOST = "127.0.0.1"` is a hardcoded constant in
  `server.py`, never a request or config parameter; CORS
  (`_LOCALHOST_ORIGIN_REGEX`) accepts only `localhost`/`127.0.0.1`
  origins. No telemetry of any kind — the only outbound call anywhere in
  the server is the Ollama health probe, itself local by default.
* **Real-time** — `GET /events/stream` (SSE) streams the same
  `domain_events` table other routes query, resumable via `since_id`
  (exclusive cursor, no gap, no duplicate — verified in
  `docs/gui/RESILIENCE_MODEL.md`). The frontend's `useEventStream.ts` hook
  consumes it with its own reconnect-with-backoff, independent of the
  health-poll-based `ConnectionContext`.
* **Errors** — every error response is `ErrorResponse` (`schema_version`,
  `code`, `title`, `detail`, `remediation`, `correlation_id`), never a raw
  traceback (`handle_unexpected_error`) — with one added discrimination
  (Phase UI-6 item 4): `sqlite3.OperationalError` gets its own handler that
  returns a specific `FH_DATABASE_BUSY` 503 for lock/busy conditions and
  falls through to the generic 500 for anything else, so "retry in a
  moment" is never given as advice for an actual bug.

## Frontend structure

* **State**: TanStack Query owns all server data (caching, refetch,
  cancellation); four small React contexts own genuinely cross-cutting
  UI state that isn't server data — `ConnectionContext` (the health-poll/
  backoff/reconnect state machine, `apps/desktop/src/connection/`),
  `EventStreamContext` (the SSE buffer, capped at 500 events),
  `InspectorContext` (what's selected in the right-hand inspector panel,
  a discriminated union so each kind owns its own fetch/render), and
  `ConsoleVisibilityContext` (the bottom console's show/hide state, added
  for the command palette's "Toggle console").
* **Navigation**: `shell/navSections.ts` is the single list every nav
  surface reads from — `SideNav.tsx`, the command palette, and the
  capability-gating logic (`resolveNavVisibility`: `absent` capability →
  hidden, `partial` → shown with an "EXP" badge, everything else →
  visible) all derive from the same array, so they can't drift apart.
* **i18n**: `react-i18next`, French and English, French is the documented
  default (FABLE §7). Every user-facing string in every screen goes
  through `t()` — confirmed screen-by-screen during this session's
  Playwright verification passes, which always ran a French-default check
  and an English-switch check.
* **Charts/graphs**: `@xyflow/react` (React Flow) for CANON's genealogy
  tree and SEMANTICS' neighbor graph; ECharts (tree-shaken via
  `echarts/core`, wrapped in `apps/desktop/src/charts/EChart.tsx`) for
  WEATHER's sparklines and radar/history charts. Both libraries are
  lazy-loaded per screen (`React.lazy()`) so no other screen's first
  paint pays for either.
* **Design system**: `apps/desktop/src/styles/` — `tokens.css` (the real
  Pomegranate Interactive palette, sourced verbatim from
  `PomegranateInteractive_Site`, two text colors later nudged for WCAG AA
  per `docs/gui/ACCESSIBILITY_MODEL.md`), `typography.css`,
  `surfaces.css` (panels, hairlines, the global `:focus-visible` ring),
  `motion.css` (the app's entire motion surface, all gated behind
  `prefers-reduced-motion`). See `docs/gui/DESIGN_SYSTEM.md`.

## Managed backend (desktop only)

`src-tauri/src/backend.rs` is the one place this app spawns a process, and
deliberately not via the Tauri shell plugin's scoped-command mechanism —
three fixed `#[tauri::command]` functions (`launch_backend`,
`stop_backend`, `backend_is_managed`) with a fully-typed argument list,
argument-validated (`validate_repo_root`/`validate_python_bin`,
`Path::canonicalize` against real marker files) before anything spawns,
and always via `std::process::Command::new(...).args([...])` — never a
shell, so there is no command-injection surface. A pure `backend_args()`
function is unit-tested (`cargo test`) to prove no argument position can
ever carry a host/bind override, for any input. The backend it spawns is
a real, user-owned Python + Field Horizon checkout, never a bundled
sidecar binary and never bundled ML models — see
`docs/gui/PACKAGING.md` for exactly what does and doesn't ship in the
built artifacts.

## What each hardening item actually changed

Phase UI-6 audited before touching anything, every time — see each item's
own doc for the specific gap found and fixed: `SECURITY_MODEL.md`,
`PERFORMANCE_MODEL.md`, `ACCESSIBILITY_MODEL.md`, `RESILIENCE_MODEL.md`,
`EXPORTS_MODEL.md`, `PACKAGING_MODEL.md`.
