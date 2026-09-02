# UI Feature Coverage — Finalized (Phase UI-7)

Every screen actually shipped, what real backend capability it exposes,
and how it was verified. This replaces the Phase UI-1 planning skeleton —
every "endpoint gap" that document listed has since been closed; this is
the as-built map, not the plan.

| Screen | Real backend capability exposed | Key route(s) | Verified by |
|---|---|---|---|
| **COMMAND** | Engine-wide stats, recent cycles, live event feed, pipeline stage list | `GET /status`, `GET /canon` (recent), `GET /events`, `GET /events/stream` | vitest + live Playwright pass |
| **CORPUS** | Source listing (server-paginated + searched), source detail, chunk browsing, manifest view, manifest-driven ingest with live SSE progress | `GET /sources`, `GET /sources/{id}`, `GET /sources/{id}/chunks`, `GET /manifest`, `POST /ingest/manifest` | vitest + live pass against a synthetic 3,000-source DB (Phase UI-6 item 2) |
| **RETRIEVAL** | Hybrid retrieval v2 with full score-component explain, weight overrides, presets | `POST /retrieve` | Live pass, real query against real corpus |
| **PLANNER** | Retrieval planner v3 classification + strategy selection preview | `POST /planner/preview` | Live pass |
| **CYCLES** (launcher) + **LIVE CYCLE** | Single- and multi-agent cycle launch, full SSE timeline (interpreter → retrieval → agents → synthesis → evaluation → critic/rewrite → canon) | `POST /cycle`, `POST /multi-cycle`, `GET /events/stream?run_id=` | E2E suite (`e2e/full-workflow.spec.ts`) genuinely launches one real cycle |
| **SEMANTICS** | Top semantic nodes per kind, neighborhood graph (capped, real) | `GET /semantics/top`, `GET /semantics/neighbors` | Live pass; real production `tag_concepts` corpus tagging run to seed real data |
| **CANON** | Current/as-of canon, why-changed genealogy, real cycle genealogy tree (React Flow) | `GET /canon`, `GET /why-changed/{id}` | Live pass; React Flow overflow bug found and fixed live |
| **WEATHER** | Deep-vs-surface axis readings, sparkline history, school overlays, radar + history chart PNG export | `GET /weather` | Live pass; PNG export verified as a real ~100KB image |
| **SCHOOLS** | Clustering runs (real, triggerable), membership, cross-run lineage, distances | `GET /schools`, `GET /schools/all`, `GET /schools/{id}/members`, `GET /schools/distances`, `POST /schools/run` | Live pass against a disposable scratch install with synthetic embeddings (dev DB had zero clustered schools) |
| **COUNCILS** | Council history, rehabilitation/overturn linkage to canon events | `GET /councils`, `GET /councils/{id}` | Live pass; real `provenance backfill`/`backfill-temporal-canon` CLI runs against the dev DB to recover historically-missing links |
| **DREAMS** | Dream-run ledger, ontology proposal review (apply with real parity check + git commit, reject), both now confirmed *and* event-logged | `GET /dreams`, `GET /dreams/{stamp}`, `GET /proposals`, `GET /proposals/{n}`, `POST /proposals/{n}/apply`, `POST /proposals/{n}/reject` | Live pass; Phase UI-6 item 1 added the missing confirm+event-log on reject |
| **PROVENANCE** | Lineage tree, supply-chain report, cross-links, **reproducible cycle bundle export** (JSON/Markdown) | `GET /lineage/{id}`, `GET /provenance/{id}`, `GET /cycles/{id}`, `GET /cycles/{id}/evidence`, `GET /config` | Live pass against real cycle #6 ("the machine as idol") — real query/model/17 evidence rows |
| **CONTRADICTIONS** | Live-computed pressure-edge contradiction ranking, CSV/JSON export | `GET /export/contradictions` | Live pass; capability banner honestly states partial-detection scope |
| **EVALUATION** | Live re-evaluation preview (symbolic density, doctrinal enforcement, structure/length scores, verdict) | `POST /evaluation/preview` | Live pass |
| **MODELS** | Model/prompt/embedding registry listing and detail | `GET /registry/{kind}`, `GET /registry/{kind}/{id}` | Live pass |
| **SYSTEM** | Live health checks (db/schema/model_provider/event_channel, each independently), capability matrix | `GET /health`, `GET /capabilities`, `GET /system-info` | Live pass; model-provider check extended (Phase UI-6 item 4) to detect "reachable but not pulled," not just reachability |
| **SETTINGS** | Read-only current-config view (no write path — config.yaml is hand-edited by design) | `GET /system-info`, `GET /config` | Live pass |
| **EMERGENCE MONITOR** | Not built — the repository audit found no real Basilisk/emergence/anomaly/recursion/attractor detection logic to visualize. `docs/gui/EMERGENCE_MONITOR_SPEC.md` documents detection criteria for if/when that logic is ever added, per FABLE §19's own conditional instruction. | — | N/A — a spec, not a screen |
| **Command palette** (§11) | Real navigation to every visible screen, language switch, console toggle, "open latest cycle," "restart managed backend" (confirmed), hand off to global search | Ctrl/Cmd+K | vitest + live pass |
| **Global search** (§12) | Grouped real results: Sources, Chunks/results (via `/retrieve`, not a second engine), Canon/cycles, Contradictions, Models, recent Events, plus a direct lineage/provenance id jump | Ctrl/Cmd+Shift+K | vitest + live pass against real corpus |

## Explicitly not covered, and why

* **Semantic-profile and settings/params categories in global search** —
  no free-text-searchable data model exists for either; settings is a
  fixed navigation target already reached via the palette.
* **SVG/PNG export for React Flow graphs** (CANON, SEMANTICS) — would
  need a new dependency (`html-to-image` or similar) and non-trivial
  layout capture; ECharts' native export covered WEATHER with zero new
  dependencies, which is what this project actually shipped.
* **Windows packaging** — no toolchain available in this build
  environment; documented, not faked.
* **A cycle's original per-component evaluation breakdown**, in the
  reproducible cycle bundle — never persisted anywhere by the engine
  itself (only the rolled-up verdict/score survive); the bundle includes
  a live recomputation instead, honestly labeled `recomputed_now: true`.

See each hardening item's own doc
(`SECURITY_MODEL.md`/`PERFORMANCE_MODEL.md`/`ACCESSIBILITY_MODEL.md`/
`RESILIENCE_MODEL.md`/`EXPORTS_MODEL.md`/`PACKAGING_MODEL.md`) for the
full reasoning behind each of these scope boundaries.
