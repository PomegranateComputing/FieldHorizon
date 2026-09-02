# Test Strategy (Phase UI-7)

Five distinct suites, each testing what it's actually positioned to test —
no single suite tries to cover everything, and nothing here is a fixture
standing in for a real backend where a real backend was actually
available.

## Backend: pytest

**635 tests passing**, `ruff check` and `mypy` both clean across all 52
source files. Every new engine method/route added across Brief IV has
real unit and integration coverage — e.g. `tests/test_engine_consolidation.py`
for facade methods, `tests/test_server.py` for the HTTP layer (including a
full auth sweep, `test_every_get_route_requires_auth`, that every new route
gets added to). `tests/test_openapi_contract.py` guards the contract
itself (see `docs/gui/API_CONTRACT.md`).

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check fieldhorizon/ tests/
.venv/bin/mypy fieldhorizon/
```

## Rust: cargo test

**6 tests passing** (`apps/desktop/src-tauri`), all in `backend.rs`:
structural proofs that the managed-backend argv can never carry a host/
bind override for any input (`backend_args_never_contains_a_host_or_bind_override`,
`backend_args_treats_a_hostile_token_file_as_one_opaque_argument`), plus
path-validation rejection tests. This is the one place the desktop target
spawns a process, so it's the one place with dedicated Rust tests.

```bash
cd apps/desktop/src-tauri && cargo check && cargo test
```

## Frontend: vitest

**46 tests passing** across 13 files, `tsc --noEmit` clean. Mix of:

* Pure logic/rendering tests (`SideNav.test.tsx`, `TopBar.test.tsx`,
  `navSections.test.ts`).
* Real state-machine tests that drive the actual implementation, not a
  mock of its output — `ConnectionContext.test.tsx` (the real poll/
  backoff/give-up/retry cycle, using both real and fake timers as
  appropriate) and `useEventStream.test.ts` (a simulated mid-stream drop,
  asserting the reconnect resumes from the exact last event id with no
  gap or duplicate).
* Accessibility behavior (`DreamsPage.test.tsx` — role="dialog", focus
  trap, Escape, focus return, all exercised through React Testing
  Library's real DOM, not asserted structurally).
* Export correctness (`exportFile.test.ts`, `cycleBundle.test.ts` — the
  attribution block is actually present in the generated Blob content,
  not just assumed).

```bash
cd apps/desktop && npx tsc --noEmit && npx vitest run
```

## Frontend build

```bash
cd apps/desktop && npm run build
```

`tsc && vite build` — must be clean (zero type errors) before every
commit this session made; confirmed each time.

## End-to-end: Playwright, against a real backend

`apps/desktop/e2e/full-workflow.spec.ts` (`npm run test:e2e`) is **not
hermetic and not in CI on purpose** — it drives the real FastAPI backend
against whatever corpus is actually ingested, and one test genuinely
launches a multi-agent cycle through the real local model provider. There
is no mock/fixture mode for this suite; CI has no ingested corpus and no
local Ollama, so it isn't wired in there. Prerequisites and exact
invocation: `apps/desktop/README.md`'s "E2E tests" section.

## The verification discipline actually used for every UI-5/UI-6 item

Beyond the four suites above, every screen and every hardening item in
this project was additionally verified live, by hand, this session,
following one consistent pattern documented in each item's own doc
(`SECURITY_MODEL.md`, `PERFORMANCE_MODEL.md`, `ACCESSIBILITY_MODEL.md`,
`RESILIENCE_MODEL.md`, `EXPORTS_MODEL.md`, `PACKAGING_MODEL.md`):

1. Start the real backend (either the actual dev database, or a
   disposable scratch install with its own config/database when the real
   one lacked the needed data or the test was destructive/large-scale)
   and the real Vite dev server.
2. Drive it with a throwaway Playwright script — never committed,
   deleted after use — taking real screenshots and reading real network
   responses.
3. Fix whatever the live pass actually surfaced (this caught real bugs
   throughout the project: a Vite dev-proxy route missing for a new
   endpoint, a French-vs-English test assertion mismatch, `/provenance`
   never checking the target existed, a SQLite lock from holding a
   connection open across an event emit, and others — see each phase's
   commit messages).
4. Clean up completely: kill the servers, `npm uninstall playwright`
   (never a persistent dev dependency), confirm `git status` shows no
   residual diff in `package.json`/`package-lock.json`.

Two purpose-built scratch environments were used for scale/failure testing
specifically because they'd be destructive or impractical against the real
dev database: a synthetic 3,000-source/36,000-chunk database (Phase UI-6
item 2, performance) and a real `kill -9` of the backend process mid-session
(Phase UI-6 item 4, resilience) — both documented with their actual
measured results in the relevant item's doc.
