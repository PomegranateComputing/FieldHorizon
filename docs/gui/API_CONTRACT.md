# API Contract (Phase UI-7)

## Contract-first workflow

`fieldhorizon/contracts.py`'s Pydantic models are the single source of
truth for every request/response shape in this API. The frontend never
hand-writes a type against the wire format — it's generated:

```bash
# 1. Regenerate contract/openapi.json from the live FastAPI app
python -m fieldhorizon.openapi_export

# 2. Regenerate contract/types.gen.ts from that schema
cd contract && npm run generate
```

`apps/desktop/src/api/types.ts` re-exports the specific generated types
each screen needs (`export type SourceModel = components["schemas"]["SourceModel"]`, etc.) rather than importing the raw generated file everywhere.

`tests/test_openapi_contract.py::test_frozen_contract_matches_the_live_schema`
runs on every backend test invocation and fails if `contract/openapi.json`
doesn't byte-match what the live app actually serves — a contract change
without regenerating both files is a broken build, not a silent drift.
Both generated files are committed (not gitignored), so a contract change
shows up as a real, reviewable diff.

## Auth

Every route except none requires `Authorization: Bearer <token>` — see
`docs/gui/SECURITY_MODEL.md` and `docs/gui/TOKEN_FLOW.md` for where the
token comes from and how each delivery target obtains it.

## Error shape

Every error response is `ErrorResponse`:

```json
{
  "schema_version": 1,
  "code": "FH_SOURCE_NOT_FOUND",
  "title": "Source not found",
  "detail": "No source with id 999.",
  "remediation": "Check GET /sources for valid ids.",
  "correlation_id": "..."
}
```

`code` is a stable, greppable identifier (`FH_HTTP_404`, `FH_DATABASE_BUSY`,
`FH_INTERNAL_ERROR`, `FH_CYCLE_NOT_FOUND`, ...), never a raw exception
message or traceback (`fieldhorizon/server.py`'s
`handle_unexpected_error`/`handle_db_locked`/`handle_fh_error`/
`handle_http_error`).

## Route inventory

Every real route this app added or extended, grouped by the screen that
calls it. (This is a map of what exists, not a tutorial — see each
screen's own component for exact usage.)

| Route | Screen(s) |
|---|---|
| `GET /health`, `GET /capabilities`, `GET /system-info`, `GET /config` | SYSTEM, SETTINGS, startup/connection sequence |
| `GET /status`, `GET /events`, `GET /events/{event_id}`, `GET /events/stream` | COMMAND, the bottom console, LIVE CYCLE |
| `GET /sources`, `GET /sources/{id}`, `GET /sources/{id}/chunks` | CORPUS, Inspector |
| `GET /manifest`, `POST /ingest/manifest` | CORPUS (manifest/ingest tabs) |
| `POST /retrieve`, `POST /planner/preview` | RETRIEVAL, PLANNER |
| `POST /cycle`, `POST /multi-cycle` | CYCLES launcher |
| `GET /canon`, `GET /why-changed/{cycle_id}`, `GET /cycles/{cycle_id}`, `GET /cycles/{cycle_id}/evidence` | CANON, PROVENANCE's reproducible cycle bundle |
| `GET /lineage/{target_id}`, `GET /provenance/{target_id}` | PROVENANCE |
| `GET /export/contradictions` | CONTRADICTIONS, the cycle bundle's (unscoped) snapshot |
| `GET /export/factions`, `GET /export/doctrines`, `GET /export/beliefs`, `GET /export/weather` | Godot-facing exports, not GUI screens |
| `GET /semantics/top`, `GET /semantics/neighbors` | SEMANTICS |
| `GET /weather` | WEATHER |
| `GET /schools`, `GET /schools/all`, `GET /schools/{id}/members`, `GET /schools/distances`, `POST /schools/run` | SCHOOLS |
| `GET /councils`, `GET /councils/{id}` | COUNCILS |
| `GET /dreams`, `GET /dreams/{stamp}`, `GET /proposals`, `GET /proposals/{n}`, `POST /proposals/{n}/apply`, `POST /proposals/{n}/reject` | DREAMS |
| `POST /evaluation/preview` | EVALUATION, the cycle bundle's recomputed-evaluation section |
| `GET /registry/{kind}`, `GET /registry/{kind}/{id}` | MODELS |
| `GET /fingerprint/{source_id}` | Inspector (source fingerprint) |
| `GET /metrics` | SYSTEM |

Three routes were added specifically for Phase UI-6 item 6's reproducible
cycle bundle and didn't exist before it: `GET /cycles/{cycle_id}` (there
was no single-cycle detail lookup — only `/canon`, filterable by verdict,
never by id), `GET /cycles/{cycle_id}/evidence` (the verbatim
`cycle_sources` rows `fieldhorizon/replay.py`'s exact-reproduction path
already read, now reachable from the API), and `GET /config` (exposing
`engine.config()`, which existed with no route).

## Pagination

`GET /sources` is the one endpoint genuinely at risk of unbounded growth
(FABLE §15's "thousands of sources") and takes real `limit`/`offset`/`q`
query params, server-clamped to `[1, 500]` regardless of what's requested,
with a `total` field in the response. See `docs/gui/PERFORMANCE_MODEL.md`
for why the other list endpoints were deliberately left unbounded (small,
naturally-capped tables) rather than over-engineered.
