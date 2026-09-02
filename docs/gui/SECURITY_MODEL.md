# Security Model (Phase UI-3 item 1, hardened in Phase UI-6 item 1)

FABLE §14's threat model, as actually implemented, not aspirational.

## What this app is

A local-first control surface for a single-user, single-machine Field Horizon
installation. There is no multi-tenant concern, no remote attacker model in
the internet sense — the realistic threats are: another local process or
browser tab reaching the API, a compromised or careless frontend dependency,
and a frontend bug that lets the wrong thing get deleted or executed.

## Backend binding and network exposure

* `HOST = "127.0.0.1"` is hardcoded in `fieldhorizon/server.py`, never a
  request or config parameter — see `test_run_server_binds_to_loopback_only`
  and `test_host_constant_is_loopback`.
* CORS (`fieldhorizon/server.py`'s `_LOCALHOST_ORIGIN_REGEX`) allows only
  `http://localhost(:port)` and `http://127.0.0.1(:port)` origins — see
  `test_cors_middleware_allows_only_localhost_origins`.
* No telemetry: the only outbound HTTP call anywhere in `fieldhorizon/server.py`
  or `fieldhorizon/diagnostics.py` is the model-provider health check against
  `cfg.ollama_base_url`, itself local by default.
* Every route requires the bearer token (`HTTPBearer` + `require_token`,
  compared with `secrets.compare_digest`) — see the auth-sweep tests in
  `tests/test_server.py`.

## Token handling (desktop and web, item 2 covers the web flow in full)

* The token is generated/read by `load_or_create_token`, written to a file
  with `chmod 600`, and is never bundled into any built frontend asset —
  both the Tauri app and the browser build read it at runtime from wherever
  the user points them, never from a compiled-in constant.
* Never logged: `fieldhorizon/server.py`'s request-logging middleware records
  path/method/status/elapsed_ms only, never headers.

## Tauri capabilities (`apps/desktop/src-tauri/capabilities/default.json`)

Only four permissions are granted, all to the `main` window:

* `core:default` — the standard window/app/event baseline every Tauri app needs.
* `dialog:allow-open`, `dialog:allow-save` — file/directory pickers (corpus
  ingestion paths, exports).
* `dialog:allow-confirm` — native confirm dialogs for destructive actions
  (§14's "confirmations pour les suppressions").

No `shell`, `fs`, or `http` plugin is registered at all. There is deliberately
no generic shell-exec or arbitrary-file-access surface reachable from the
frontend.

## Managed backend (`apps/desktop/src-tauri/src/backend.rs`)

The one place this app spawns a process. Deliberately **not** implemented via
the Tauri shell plugin's scoped-command mechanism, because that still passes
a command matched against a scope; instead it's three fixed `#[tauri::command]`
functions with a fully-typed argument list (`python_bin`, `repo_root`, `port`,
`token_file` — never a free-form string the frontend could shape into an
arbitrary invocation):

* `launch_backend` validates `repo_root` resolves to a real directory
  containing `config.yaml` and `pyproject.toml` before spawning anything, and
  validates `python_bin` resolves to a real file. Both use
  `Path::canonicalize`, so a `..`-laden path either resolves to a real
  location that still has to pass the marker-file check, or fails outright —
  there's no shell involved to reinterpret it.
* The child is spawned via `std::process::Command::new(...).args([...])`,
  never a shell (`sh -c`), so there is no command-injection surface: every
  argument is a discrete argv entry.
* `stop_backend` only ever kills the `Child` handle *this instance* stored
  after spawning it. If nothing is tracked (attached to an externally-running
  backend, or never launched), it returns an error rather than searching for
  and killing an unrelated process — satisfying §8.4's "ne jamais tuer
  arbitrairement un processus externe non lancé par lui."
* `backend_is_managed` reaps an already-exited child via `try_wait` so a
  crashed process doesn't keep reporting as managed.
* The exact argv passed to the child is built by a pure, spawn-free
  `backend_args(port, token_file)` function, unit-tested (`cargo test`) to
  prove no argument position can ever carry a host/bind override
  (`--host`, `-h`, `--bind`, `0.0.0.0`, `::`) for any port or token-file
  input, including adversarial token-file strings crafted to look like a
  flag — a structural guarantee that this side of the boundary can't
  regress into binding off-loopback, mirroring the Python-side
  `test_run_server_binds_to_loopback_only` guarantee on the other side of
  the same command line.

## Web build

The `/ui` browser target (item 2) has no access to the platform adapter's
`backend` controls at all (`webPlatform.backend === null`) and no file-dialog
capability (`openFileDialog`/`saveFileDialog` throw a clear "not available in
the browser build" error) — a browser tab cannot spawn a local process or
read arbitrary filesystem paths, and the adapter makes that explicit instead
of silently failing.

## Destructive actions

This app's two clearest destructive actions are DREAMS' proposal **apply**
(merges a proposal into `ontology.yaml` and git-commits it) and **reject**
(moves a proposal out of the pending queue into `proposals/rejected/`). Both
now satisfy §14's "confirmations pour les suppressions" end to end:

* Both route through `PlatformAdapter.confirm` (native `dialog:allow-confirm`
  on desktop, `window.confirm` on web) before anything happens — apply via
  `ApplyConfirmDialog`, reject via a direct `platform.confirm(...)` call in
  `DreamsPage.tsx`'s `handleReject`.
* Both are event-logged: `apply_proposal`/`reject_proposal`
  (`fieldhorizon/proposals.py`) emit `ProposalApplied`, `ProposalRejected`,
  or `ProposalApplyFailed` domain events (aggregate `"proposal"`, keyed by
  proposal number) via the same `OperationEmitter` fabric every other
  domain event uses, tagged with the real actor (`ACTOR_CLI` from the CLI,
  `ACTOR_SERVER` from the API) — see `tests/test_proposals.py`'s
  `test_reject_proposal_emits_a_domain_event` and the apply-path event
  assertions.

No other action in the built app deletes or irreversibly overwrites data —
ingestion, dream runs, cycles, and clustering are all additive.

## Known gaps at this phase

* No backup-before-migration step exists yet (§14's "sauvegarde avant
  migration sensible") — there is no migration flow in the UI yet to attach
  it to.
