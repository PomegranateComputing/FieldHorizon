# Resilience Model (Phase UI-6 item 4)

The scenarios this item names: backend killed mid-cycle (UI degrades,
reconnects, resumes the event stream), a locked/busy database, an absent
model, empty results, long timelines, and cancelled requests. Audited first,
then fixed the two genuine gaps found, then verified live.

## What the audit found already in place

* **Backend-drop reconnection** (`connection/ConnectionContext.tsx`): a real
  poll/backoff/give-up state machine — 10s steady-state polling, a
  `[1,2,4,8,16,30,30,30]`s backoff schedule on failure, giving up after 8
  consecutive failures (`phase: "disconnected"`) with a manual `retry()` as
  the only way back in. `ConnectionBanner.tsx` renders a real per-phase UI
  (reconnecting with attempt count, degraded, disconnected-with-retry-button)
  — already covered by `ConnectionBanner.test.tsx`'s 5 phase cases, though
  only at the "given this phase, render what" level, not the state machine
  producing that phase.
* **Event-stream resume** (`hooks/useEventStream.ts`): reconnects with
  backoff and resumes from the real last-seen event id via `since_id`, both
  on a clean stream-end and on a mid-read failure. Backend side
  (`server.py`'s `/events/stream`), the cursor is strictly the client's own
  `since_id` and advances per emitted event before checking for client
  disconnect — genuinely no-gap, no-duplicate replay.
* **Empty results**: spot-checked COUNCILS, WEATHER, CANON, CONTRADICTIONS —
  all four already render a real "no data yet" message (or, for WEATHER,
  literal `--` placeholders) rather than a blank screen or crash. Confirmed
  live against a genuinely fresh, empty scratch database.
* **Cancelled requests**: covered by Phase UI-6 item 2
  (`docs/gui/PERFORMANCE_MODEL.md`) — `getSources`/CorpusPage's
  `useInfiniteQuery` threads an `AbortSignal` into `apiFetch`, verified live
  to actually cancel in-flight requests, not just reduce how many fire.
  Not re-verified here; that item's verification already covers this
  scenario.

## What was a genuine gap, and the fix

* **DB busy/locked had no busy-timeout at all.** `fieldhorizon/db.py`'s
  `connect()` set `PRAGMA foreign_keys = ON` but never a busy timeout —
  SQLite's own default is 0, so a second connection hitting a writer
  mid-transaction (the CLI running a dream/ingest while the server also
  touches the DB, a completely realistic case for this single-machine app)
  failed instantly with "database is locked" instead of waiting a moment.
  Fixed: `connect()` now also sets `PRAGMA busy_timeout = 5000`.
* **A locked/busy DB, if it still happened, surfaced as an opaque generic
  500.** `server.py` now has a dedicated `sqlite3.OperationalError` handler
  that returns a specific `FH_DATABASE_BUSY` 503 with "retry in a moment"
  guidance *only* when the message actually says "locked"/"busy" — any other
  `OperationalError` (malformed SQL, a missing table — a real bug, not a
  transient condition) still falls through to the same generic 500, so
  "retry in a moment" is never given as wrong advice for an actual defect.
* **Model-absence detection only checked reachability, not the specific
  model.** `diagnostics.py`'s `_check_model_provider` called Ollama's
  `/api/tags` and treated any 200 response as "ok" — "Ollama process down"
  was visible, but "Ollama running, the configured model never pulled" (a
  genuinely easy setup mistake) was invisible. Fixed: it now parses the
  tags list and checks `cfg.default_model` against it, returning `degraded`
  with `"Ollama reachable but '<model>' is not pulled -- run \`ollama pull
  <model>\`."` when it's missing. Verified live against the real local
  Ollama instance (already running, with `hermes3:8b` genuinely pulled) by
  pointing a scratch config at `nonexistent-model:1b` — the `/health`
  response came back exactly as designed, reachability real, absence real,
  message real.

## Test coverage added

* `tests/test_db_canon.py::test_connect_sets_a_nonzero_busy_timeout` —
  queries `PRAGMA busy_timeout` back after `connect()` and asserts it's
  nonzero.
* `tests/test_server.py::test_database_locked_returns_a_clean_503_not_a_raw_500`
  and `::test_other_operational_errors_still_return_a_generic_500` — the
  first forces a `sqlite3.OperationalError("database is locked")` and
  checks for `FH_DATABASE_BUSY`/503; the second forces an unrelated
  `OperationalError` and checks it still gets the generic `FH_INTERNAL_ERROR`
  /500, proving the handler discriminates rather than blanket-catching.
* `tests/test_server.py::test_health_degrades_when_configured_model_is_not_pulled`
  — mocks a real-shaped `/api/tags` response missing the configured model,
  asserts `degraded` with the model name and "not pulled" in the detail.
  (`test_health_endpoint_is_ok_by_default` was also fixed to mock its own
  `/api/tags` response explicitly — it previously passed only because the
  literal dev machine running the suite happens to have `hermes3:8b`
  pulled, an accidental environment coupling this change would have made
  worse; it's now a properly isolated unit test.)
* `apps/desktop/src/connection/ConnectionContext.test.tsx` (new) — exercises
  the *real* `ConnectionProvider`, not a mock of its return value: one test
  drives it through connected → reconnecting → connected again (the literal
  "backend killed mid-cycle... reconnects" scenario) using real timers;
  a second, using fake timers, drives the full 8-attempt backoff schedule
  through to `disconnected` and confirms a manual `retry()` is genuinely
  the only way back in.
* `apps/desktop/src/hooks/useEventStream.test.ts` (new) — mocks `fetch`
  with a `ReadableStream`-shaped body that yields two SSE frames and then
  throws mid-read (simulating the connection dying), then mocks a second
  `fetch` call and asserts its URL contains `since_id=2` (the last event
  actually received) and that the final event list is `["a","b","c"]` with
  no gap or duplicate.

## Live verification

Against a disposable scratch install (own config, own empty DB, deleted
after):

* Killed the real backend process (`kill -9`) mid-session. The UI correctly
  degraded: status flipped to `FAILED`, a `RECONNEXION EN COURS (TENTATIVE
  N)...` banner appeared (first observed at t=20s, matching the 10s poll
  interval plus first backoff step), and COMMAND kept showing its last-known
  data rather than going blank.
* Restarted the backend and confirmed via direct `curl` (bypassing the
  frontend entirely) that it was genuinely healthy and reachable again on
  the same port/token.
* Same-session recovery back to `connected` (as opposed to reloading the
  page, which trivially reconnects fresh) could not be observed live to
  completion in this pass — the disposable dev-server processes this
  verification depends on were killed by the sandbox environment itself
  partway through a ~90-second observation window, twice, for reasons
  unrelated to the app (nothing in `nohup`'s own log pointed to an app-side
  crash; the Vite dev server process was the one that disappeared, not the
  page or the backend). The state machine driving that recovery is the same
  one `ConnectionContext.test.tsx` proves deterministically — the live pass
  independently confirmed the degrade half of the story (the harder half to
  fake), and the reachability of the restarted backend, but not the full
  live round-trip in one continuous session. Worth re-attempting outside
  this environment's constraints rather than claiming more than what was
  actually observed.
* Model-absence: pointed a scratch config's `default_model` at
  `nonexistent-model:1b` against the real, already-running local Ollama —
  `/health` correctly returned `degraded` with the exact designed message.
* Empty results: fresh, genuinely empty scratch database — COUNCILS,
  WEATHER, and CANON all rendered real empty-state copy, screenshotted.

## Known gaps

* Locked-DB recovery itself was not exercised live (holding a real
  concurrent write transaction open against the same file while the server
  reads) — the fix (`busy_timeout` + the specific 503) is unit-tested but
  not end-to-end verified under genuine concurrent access.
* Same-session backend-recovery (not a page reload) was not observed live
  to completion, per above — covered deterministically by
  `ConnectionContext.test.tsx` instead.
