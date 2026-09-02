# Runbook (Phase UI-7)

Exact commands. Everything below was actually run, this session, exactly
as written.

## Install dependencies

```bash
# Backend (from repo root)
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# Frontend
cd apps/desktop && npm install
```

## Run in development

**One command, both halves, from the repo root:**

```bash
./scripts/field-horizon-ui
```

Checks prerequisites (a real `config.yaml`, an importable `fieldhorizon`
package, `npm` on `PATH`), installs frontend deps if `node_modules` is
missing, then runs the backend (`field-horizon serve`) and `npm run dev`
together with prefixed, unbuffered logs (`[backend]` / `[frontend]`), and
tears down the whole process tree on Ctrl-C — no orphaned child
processes. Override the port/token file with
`FIELD_HORIZON_PORT`/`FIELD_HORIZON_TOKEN_FILE` env vars.

Or run each half by hand:

```bash
# Backend
.venv/bin/python -m fieldhorizon.cli serve --port 8777 --token-file .fh_token

# Frontend (separate terminal)
cd apps/desktop && npm run dev
```

Open `http://localhost:1420`. The web build's `TokenGate` will ask for the
token — `cat .fh_token` and paste it in (see `docs/gui/TOKEN_FLOW.md`).
The Tauri desktop build (`cd apps/desktop && npm run tauri dev`) instead
shows a startup screen that can launch a managed backend itself, given a
Python interpreter path and this repo's path.

## Run tests

```bash
# Backend: pytest + lint + types
.venv/bin/python -m pytest -q
.venv/bin/ruff check fieldhorizon/ tests/
.venv/bin/mypy fieldhorizon/

# Frontend: types + unit/component tests
cd apps/desktop
npx tsc --noEmit
npx vitest run
# or the wrapper script (installs node_modules first if missing):
../../scripts/gui-test

# Rust (only the desktop target's own small surface)
cd apps/desktop/src-tauri && cargo check && cargo test

# Contract: fails if contract/openapi.json has drifted from the live schema
.venv/bin/python -m pytest -q tests/test_openapi_contract.py

# E2E (real backend, real corpus, real model provider -- see e2e/README notes)
cd apps/desktop && npm run test:e2e
```

## Build

```bash
cd apps/desktop && npm run build
# or: ../../scripts/gui-build
```

Produces `apps/desktop/dist/` — both the static assets `fieldhorizon/server.py`
mounts at `/ui`, and the frontend half of what `tauri build` packages for
desktop.

## Package (Linux)

```bash
cd apps/desktop
npm run build
npx tauri build
```

See `docs/gui/PACKAGING.md` for exactly what's produced and what's
inside.

## Regenerate the API contract (after any backend contract change)

```bash
python -m fieldhorizon.openapi_export
cd contract && npm run generate
```

## Connect an existing backend

If a `field-horizon serve` instance is already running elsewhere:

* **Browser (`/ui` or `http://localhost:1420` in dev)**: open it, paste
  the token from that instance's token file into the `TokenGate` screen.
* **Desktop (Tauri)**: the startup screen's managed-launch flow expects to
  spawn its own backend; to attach to one already running instead, the
  app still needs a token — same `TokenGate` mechanism, reused from the
  web flow (see `docs/gui/TOKEN_FLOW.md`).

## Change the database

Edit `config.yaml`'s `paths.database` (relative to the repo root unless
absolute) and restart the backend. Nothing in this GUI writes to
`config.yaml` — it's hand-edited, same "curation as code" discipline as
`data/sources.yaml`. `GET /system-info` and `GET /config` both reflect
whatever's actually in the file after a restart.

## Configure Ollama

`config.yaml`'s `ollama:` block — `base_url`, `default_model`,
`temperature`, `top_p`, `repeat_penalty`, `num_ctx`. `GET /health`'s
`model_provider` check will report:

* `ok` — Ollama reachable and `default_model` is actually pulled.
* `degraded` with `"Ollama unreachable: ..."` — the process isn't running
  or `base_url` is wrong.
* `degraded` with `"... is not pulled -- run \`ollama pull <model>\`"` —
  Ollama is running but the configured model was never pulled (see
  `docs/gui/RESILIENCE_MODEL.md`).

## Diagnose a failure

1. SYSTEM screen (or `GET /health` directly) — shows `db`/`schema`/
   `model_provider`/`event_channel` status individually, each with a real
   `detail` string, not just an overall pass/fail.
2. The bottom console (or `GET /events`) — the actual domain-event history
   for whatever operation just ran.
3. `docs/gui/TROUBLESHOOTING.md` for the specific symptoms already seen
   and fixed during this project.

## Read logs

The backend logs to its own stdout (Python's `logging`, `RichHandler`,
`WARNING` level by default) — when started via `./scripts/field-horizon-ui`
these lines are prefixed `[backend]`; started by hand, they're whatever
terminal it's running in. The frontend's own errors surface in the
browser/Tauri devtools console (`Ctrl+Shift+I` in most Tauri builds) — the
app itself never silently swallows an error; every `ApiError` carries a
`code`/`detail`/`remediation` visible in the UI, and the raw response is
always in the browser's network tab too.
