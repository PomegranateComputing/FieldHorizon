# Performance Model (Phase UI-6 item 2)

FABLE §15's requirement, audited against what actually exists rather than
applied as a blanket rewrite. The client must stay fluid with thousands of
sources, tens of thousands of chunks, long timelines, many events, and large
graphs — using server pagination, virtualization, progressive loading,
controlled caching, cancellable requests, debounce, deferred rendering, graph
aggregation, lazy screen loading, and event streaming, without ever
transferring the whole database to the frontend to render one table.

## What the audit found already in place

* **Event streaming**: `GET /events/stream` (SSE) is the only source of the
  running event console, already cancellable (`useEventStream.ts`'s
  `AbortController`, aborted on unmount) and already capped at
  `MAX_BUFFERED_EVENTS = 500` client-side — a session can run indefinitely
  without the console's DOM or memory growing unbounded. 500 plain `<li>`
  rows is well within React's comfortable render cost; virtualizing this
  list would add complexity for no measured benefit, so it was left as is.
* **Lazy screen loading**: WEATHER, CANON, and SEMANTICS (the chart/graph-
  heavy screens) are already `React.lazy()`-loaded — no other screen pays
  for echarts or React Flow on first paint.
* **Controlled cache**: TanStack Query already provides this app's caching
  layer everywhere; nothing bypasses it with ad hoc `fetch` + local state.
* **Graph aggregation**: CANON's genealogy graph and SEMANTICS' neighbor
  graph are both server-capped (`/canon`'s `limit` default 50, `/semantics/
  top` and `/semantics/neighbors` capped at 20/15) — real caps already
  enforced before this item, not changed here.
* **Lineage/provenance recursion safety**: `build_axiom_lineage_tree` and
  `build_cycle_genealogy_tree` (`fieldhorizon/lineage.py`) already carry a
  `visited` set threaded through their recursion, so a circular ancestry
  (which `provenance.circular_ancestry` explicitly detects and surfaces)
  cannot cause unbounded recursion or duplicate subtrees. Real ancestries
  are bounded by how many revisions/genealogy edges a single item actually
  has — not by total table size — so this was audited rather than rebuilt:
  the pathological case (a cycle) is already guarded; the routine case is
  already small.

## What was a genuine gap, and the fix

The one endpoint that returned an entire table with no bound at all was
`GET /sources` — the literal "thousands of sources" case FABLE names.
`CorpusPage.tsx`'s sources tab fetched every row in one response and did
every bit of search/sort/scroll handling against that full array in memory.

* `fieldhorizon/engine.py`'s `sources()` now takes `limit` (clamped to
  `[1, 500]` server-side regardless of what a caller asks for), `offset`,
  and `q` (a `LIKE`-based substring filter over title/source_type, pushed
  into SQL rather than filtered in Python after the fact); `sources_count()`
  returns the real total for the same filter. `GET /sources` exposes all
  three as query params and `SourcesResponse` gained a `total: int` field.
* `CorpusPage.tsx`'s sources tab now uses `useInfiniteQuery` — pages of 200,
  loaded on an explicit "Load more" click, search debounced 300ms
  (`useDebouncedValue`) before it changes the query key, and the query's
  `AbortSignal` is threaded all the way into `apiFetch` so a debounced
  search change (or navigating away) cancels whatever page fetch is still
  in flight rather than risking a stale response overwriting a newer one.
  The existing `@tanstack/react-virtual` list virtualization is unchanged
  and now renders whatever's been loaded so far; sorting is honestly scoped
  to "sorted within what's loaded," not silently wrong across unfetched
  pages.
* `getSources()` in `api/client.ts` accepts `{ limit, offset, q }` and an
  `AbortSignal`; cancellation was wired specifically into this call rather
  than rewritten into all ~30 API functions — most (`/health`, `/status`,
  `/metrics`, single-item lookups) return small, fixed-size payloads where
  an in-flight abort has no measurable value. The mechanism (`apiFetch`
  already forwards any `signal` passed via `RequestInit`) is there to reuse
  wherever a future screen needs it.

## Synthetic large-DB verification

A disposable scratch install (own `config.yaml`, own SQLite file under
`/tmp`, never the real dev database) was seeded directly via SQL —
**3,000 sources, 36,000 chunks** (12 per source) — then exercised through
the real backend and a real frontend dev server, verified with Playwright:

| Measurement | Result |
|---|---|
| `GET /sources?limit=200&offset=0` (curl, server-side only) | ~13ms |
| `GET /sources?q=...` substring search (curl) | ~11ms |
| `GET /sources/1/chunks?limit=200` (curl) | ~12ms |
| CorpusPage sources tab: first paint of page 1 (200 of 3,000) | 377ms |
| Scrolling the virtualized list 10 steps across the loaded page | 327ms |
| Typing a 21-character search term (debounced) | only **2** `/sources` requests fired, not 21 — confirms debounce + cancellation are both actually working, not just wired and unused |
| "Load more" — fetching the next 200-row page | 540ms |
| Selecting a source: Inspector's chunk list (of 36,000 total) renders | 59ms |

The scratch DB and its config were deleted after verification; `git status`
confirmed no residual diff beyond the intended source changes.

## Explicitly out of scope, and why

* `/registry/{kind}`, `/proposals`, `/dreams`, `/dreams/{stamp}`,
  `/schools/all` remain unbounded. Each is backed by a table that is small
  by construction in realistic use (a handful of model/prompt registry
  entries, as many ontology proposals or dream runs or clustering runs as a
  user has actually triggered by hand) — not the "thousands of sources /
  tens of thousands of chunks" scale FABLE names. Adding pagination here
  would be speculative engineering against a size these tables don't
  reach; if that changes, the same `limit`/`offset`/`total` pattern applied
  to `/sources` in this item is the one to reuse.
* Query cancellation was not threaded into every API function — see above.
