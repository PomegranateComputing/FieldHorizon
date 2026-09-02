# Repository Reality Audit

**Phase UI-1 — Implementation Brief IV (Command Interface)**
**Audited:** 2026-07-22, working tree at commit `d44df79` (pre-ui snapshot), branch `sprint-critic-rewrite`.

This document is the capability matrix FABLE §4 asks for, grounded in real file paths and symbols — every row was verified by reading the cited code, not inferred from naming or documentation. Columns: **Present** (fully implemented, tested, reachable today) / **Partial** (real but incomplete, narrower than the name suggests, or missing a piece FABLE assumes) / **Documented-only** (described somewhere but no working code). The rightmost column is this audit's own call on what the UI may claim — never looser than the middle three columns.

A structural finding up front, because it shapes every row below: **the existing FastAPI server (`fieldhorizon/server.py`) predates almost all of Implementation Brief III.** It exposes `GET /status /canon /lineage/{id} /provenance/{id} /weather /schools /councils /sources`, `POST /retrieve /cycle`, `GET /fingerprint/{id}`, `GET /export/{factions,doctrines,contradictions,weather,beliefs}`, and `GET /metrics` — and nothing else. Every capability Brief III added (domain events, run manifests, registries, tiered replay, the provenance graph beyond a single read, bitemporal canon as-of queries, the retrieval planner) is **CLI-only today**: real, tested, and used by the CLI, but unreachable from the API a browser or Tauri client would call. This is the single largest gap Phase UI-2 exists to close, and it is a gap of *exposure*, not of *implementation* — the underlying engine work is there.

---

## 1. Ingestion

| Feature | Present | Partial | Doc-only | Module | CLI command | Data | Exposable in UI |
|---|:-:|:-:|:-:|---|---|---|:-:|
| Book/manifesto ingestion (chunking, offsets, FTS5 indexing) | ✅ | | | `ingest.py:ingest_books/105`, `chunk_text/20` | `ingest --books` | `sources`, `chunks`, `chunks_fts` | Yes |
| JSON corpus ingestion (axioms) | ✅ | | | `ingest.py:ingest_json_corpus/177` | `ingest --json` | `json_entries`, `json_entries_fts` | Yes |
| Manifest-driven ingestion (`data/sources.yaml`, curation-as-code) | ✅ | | | `ingest.py:ingest_from_manifest/428`, `manifest.py` | `ingest-manifest` | `sources.manifest_id` | Yes — **read-only** in the UI; FABLE's own §10.3 says edits stay in the manifest file, and this repo's standing rule agrees |
| Chunk-offset backfill for pre-offset rows | ✅ | | | `ingest.py:backfill_chunk_offsets/285` | `backfill-chunk-offsets` | `chunks.char_start/char_end` | Yes, as a corpus-maintenance action |
| Duplicate detection at ingest | | ⚠️ | | `ingest.py:_delete_stale_fts_for_source/90`, `upsert_source/64` | (implicit in `ingest`) | — | Path-based only (`UNIQUE(path)`); no content-hash dedup across different paths — do not claim "detect duplicates" as FABLE §10.3 lists, only "reject re-ingesting the same path" |
| PDF ingestion | | | ❌ | — | — | — | Hide. No PDF parser anywhere in the tree. |

## 2. Retrieval (FTS, semantic, hybrid, explain)

| Feature | Present | Partial | Doc-only | Module | CLI command | Data | Exposable |
|---|:-:|:-:|:-:|---|---|---|:-:|
| FTS5 lexical search (per-source-type routing) | ✅ | | | `retrieval.py:search_one_source/122`, `expanded_terms_for_source/90` | (internal to `cycle`) | `chunks_fts` | Yes |
| Dense-vector semantic search over chunks | ✅ | | | `retrieval.py:search_books_by_embedding/146`, `storage_vectors.py:VectorStore` | (internal) | `chunk_embeddings` | Yes |
| Ontology-domain routing (`wanted_sources`, `detect_domains`) | ✅ | | | `retrieval.py:43-88` | (internal) | `ontology.yaml` | Yes, as an explain component |
| **Hybrid retrieval v2** with full `--explain` decomposition | ✅ | | | `retrieval.py:hybrid_search_chunks/415`, `ScoreComponent/376` | `retrieve <query> [--explain]` | — | Yes — **already the one capability the API exposes**: `POST /retrieve` (`server.py:226`) |
| **Retrieval planner v3** (rule-based classifier + 8 backed strategies) | ✅ | | | `retrieval_plan.py:build_retrieval_plan/119`, `plan_and_retrieve/542` | `retrieve <query> --plan [--as-of T] [--explain]` | — | **API gap**: no `/retrieve` variant accepts `plan=true` today. Needs a new endpoint or a query param on the existing one. |
| Planner `legacy` / `shadow` / `active` modes (FABLE §10.5) | | | ❌ | — | — | — | **Do not build the mode-switch UI.** These three modes do not exist as a concept anywhere in this codebase. What exists is a single opt-in `--plan` flag that runs v2 *and* the planner's additional strategies together in one call, not two parallel executions to compare. Mark PLANNER screen experimental/partial; do not imply a shadow-mode comparison the backend cannot produce. |
| CONTRADICTION retrieval strategy | ✅ | | | `retrieval_plan.py:_classify_oppositional/58`, `STRATEGY_CONTRADICTION` | `retrieve --plan` (auto-activates) | `ontology.yaml` oppositions | Yes, within the retrieval lab |
| Retrieval policy constraints (3 of 7 named in Brief III enforced) | ✅ | ⚠️ | | `retrieval_plan.py:_apply_source_diversity/507`, `_apply_canon_evidence_policy/525`; `config.py:RetrievalPolicy` | (config-driven, no CLI flag) | `config.yaml: retrieval.policy` | Yes — show exactly which 3 are enforced (`max_per_source`, `min_direct_evidence_count`, `max_synthetic_evidence_ratio`) and which 4 are not (`contradiction_coverage`, `curated_source_only`, `temporal_consistency`, `token_budget`) |

## 3. Evidence selection, scoring, agents, synthesis, evaluation, critic, rewrite (the cycle pipeline)

| Feature | Present | Partial | Doc-only | Module | CLI command | Data | Exposable |
|---|:-:|:-:|:-:|---|---|---|:-:|
| Single-cycle pipeline (interpreter-free): retrieve → synthesize → evaluate → optional rewrite → canonize | ✅ | | | `cycle.py:run_cycle/46,_run_cycle_body/66` | `cycle` | `cycles`, `cycle_sources` | Yes |
| Multi-agent cycle with query interpretation | ✅ | | | `multicycle.py:run_multi_cycle`, `interpreter.py:interpret_query/91` | `multi-cycle` | same | Yes |
| **The four adversarial agents** (FABLE §10.7 "four adversaries") | ✅ | | | `agents.py:AGENTS/20` — `THEOLOGIAN_AGENT` vs `MACHINE_AGENT`, `MYTHIC_AGENT` vs `POLITICAL_AGENT`, each with an explicit `enemy` field | (internal to `multi-cycle`) | — | Yes — this is real and exactly matches FABLE's framing, name them literally |
| Canon-fragment MMR selection (evidence diversity) | ✅ | | | `canon.py:_select_by_mmr/36` | (internal) | `canon_embeddings` | Yes, as part of retrieval explain |
| Structured evaluation (symbolic density, doctrinal enforcement, length/structure/stuffing/generic/canon-loop penalties → four-way verdict) | ✅ | | | `evaluate.py:CycleEvaluation/31`, `evaluate_cycle/140` | (internal), rendered via `evaluation_to_markdown/227` | `cycles.verdict/final_score` | Yes — full score decomposition is real and structured, ideal for the EVALUATION lab |
| Four-way verdict vocabulary | ✅ | | | `evaluate.py:204-210` — `CANON` / `USEFUL_FRAGMENT` / `HERESY` / `NOISE` | — | `cycles.verdict` | Yes, untranslated per standing rule |
| Critic pass | ✅ | | | `critic.py:critique_response/83` | `cycle --auto-rewrite` | — | Yes |
| Rewrite pass | ✅ | | | `rewrite.py:rewrite_response/80`, `category_directives/51` | `cycle --auto-rewrite` | — | Yes |
| Formal evaluation *test harness* / regression suite (FABLE §10.12: "jeux de tests, scénarios, comparaison de modèles/planners/prompts, dérive, historique") | | | ❌ | — | — | — | **Hide or spec-only.** No test-scenario runner, no model/planner/prompt comparison harness, no drift tracking exists. `replay --level 5` (model-variant diff) and the planner's own comparison are the closest real things — the EVALUATION lab should surface *those*, not invent a scenario suite. |

## 4. Provenance, lineage, supply-chain, contradictions

| Feature | Present | Partial | Doc-only | Module | CLI command | Data | Exposable |
|---|:-:|:-:|:-:|---|---|---|:-:|
| Genealogy walk (canon parent lineage + axiom pressure-edge provenance) | ✅ | | | `lineage.py:build_lineage_tree/257` | `lineage <target>` | `cycles.parent_cycle_ids`, `json_entries.raw_json` | Yes; `GET /lineage/{id}` already exists |
| **Provenance graph** (11 typed relation edges, backfillable) | ✅ | | | `provenance.py` (Brief III Phase C) | `provenance backfill/show` | `provenance_edges` | `GET /provenance/{id}` exists but is read-only single-target; `provenance backfill` (mutating) is **CLI-only**, no API route |
| Supply-chain report (completeness score, synthetic-dependency ratio, circular-ancestry, weakest link) | ✅ | | | `provenance.py:build_provenance_report` | `provenance show <id>` | derived from `provenance_edges` | Yes — `ProvenanceResponse` (`contracts.py:52`) already carries every field |
| **Contradiction detection** | | ⚠️ | | `godot_export.py:build_contradictions/97` → `ontology.py:build_pressure_edges` | `godot` (export only) | computed live, not persisted | **This is the honest answer to the brief's own audit question.** What exists is *ontological pressure between two axiom statements* (declared oppositions in `ontology.yaml`, e.g. `logos` vs `bureaucracy`), computed fresh on demand — never persisted as a standing "contradiction" record, never between two fragments/cycles/chunks, no severity typology (direct/tension/ambiguity/temporal-divergence/provenance-incompatibility/stale/false-positive per FABLE §10.11 — none of that classification exists, only one `pressure_score` float + a `reason` string). Phase C's `OPPOSES` provenance edge and Phase E's `CONTRADICTION` retrieval strategy are the *same* underlying mechanism reused twice, not two independent detectors. `CONTRADICTS` (a name Phase C reserved for something stronger) has **zero writer anywhere in this codebase.** The CONTRADICTIONS screen must say exactly this, not synthesize a typology the backend doesn't have. |
| Godot export bundle (factions/doctrines/contradictions/weather/beliefs) | ✅ | | | `godot_export.py`, `docs/godot_schema.md` | `export godot`, `GET /export/*` | JSON files | Already API-exposed |

## 5. Canon, temporal as-of, councils, schools, weather, dreams

| Feature | Present | Partial | Doc-only | Module | CLI command | Data | Exposable |
|---|:-:|:-:|:-:|---|---|---|:-:|
| Canon listing (current) | ✅ | | | `engine.py:canon()` | (via `GET /canon`) | `cycles` | Already exposed |
| **Bitemporal canon** (`canon_temporal_states`, valid-time vs transaction-time, `why-changed`) | ✅ | | | `temporal.py` (Brief III Phase D) | `canon --as-of-cycle/--as-of-timestamp`, `why-changed <id>` | `canon_temporal_states` | **CLI-only, no API route at all** |
| Historical-canon comparative replay (L6) | ✅ | | | `replay.py:replay_cycle_against_historical_canon` | `replay <id> --level 6 --as-of T` | — | CLI-only |
| Councils (heresy trials + canon audits, rehabilitation/retirement) | ✅ | | | `council.py:run_council/400` | `council` | `councils`, `canon_events` | `GET /councils` (read) exists; `council` (the mutating run) is CLI-only |
| Schools of thought (k-means clustering, lineage across runs) | ✅ | | | `schools.py:run_schools/258` | `schools --k --seed` | `schools`, `school_members` | `GET /schools` (read) exists; the clustering run itself is CLI-only |
| Doctrinal weather (axis readings, canon vs surface, history) | ✅ | | | `weather.py`, `dashboard.py` (TUI + static HTML) | `weather`, `backfill-weather` | `weather_readings`, `weather_axis_vectors` | `GET /weather` exists |
| Dream runs (budget-capped autonomous exploration → ontology proposals) | ✅ | | | `dream.py:run_dream` | `dream --budget-cycles --budget-minutes --seed` | dream ledger + summary files | **No API route at all** — this is a genuinely new screen (per Brief IV's cover text) |
| Ontology proposal review (list/show/apply/reject, parity-checked) | ✅ | | | `proposals.py` | `proposals list/show/apply/reject` | `data/ontology_proposals/` (pending/applied/rejected dirs) | CLI-only; `apply` runs a real parity check (`proposals.py:_run_ontology_parity_check/84`) and git-commits `ontology.yaml` — **the UI must show this git-commit side effect explicitly**, never silently |

## 6. Domain events, replay, registries, PrincipalContext

| Feature | Present | Partial | Doc-only | Module | CLI command | Data | Exposable |
|---|:-:|:-:|:-:|---|---|---|:-:|
| **Domain event fabric — Phase A status: fully merged.** Every cycle/council/dream/retrieval-plan operation, from CLI *and* the one existing server route (`POST /cycle`), emits an identical `DomainEvent` chain. | ✅ | | | `events.py` (Brief III Phase A) | `events list/show/trace/run` | `domain_events` | **No SSE stream, no `/events` API route at all.** Phase UI-2's SSE work is a genuine *projection of a real, complete event fabric* — not a stopgap over partial coverage. This is the good-news finding of this audit. |
| Tiered replay L1–L6 | ✅ | | | `replay.py` | `replay <id> --level 1..6` | `run_manifests` | CLI-only |
| Model/prompt/embedding registries (auto-register-on-first-sight) | ✅ | | | `registries.py` (Brief III Phase B) | `registry list/show` | `model_registry`, `prompt_registry`, `embedding_registry` | CLI-only |
| Run manifests (fingerprints, config-drift detection) | ✅ | | | `manifests.py` | (consumed by `replay --level 2`) | `run_manifests` | CLI-only |
| `PrincipalContext` seam | ✅ | | | `principal.py` (Brief III optional seam) | none | `domain_events.principal_id` | **Present only as an internal parameter default** (`LOCAL_ADMIN_PRINCIPAL`); no CLI flag, no API field, no roles/policy logic anywhere. The UI should not expose a "switch principal" control — there is nothing behind it. |
| Live Ollama model listing / connectivity check | | ⚠️ | | `registries.py:_resolve_model_digest` (per-model digest lookup only, falls back silently if unreachable) | none | — | **No "list available models" or "check provider health" capability exists.** MODELS screen (FABLE §10.13) needs new backend work, not just a UI wrapper. |

## 7. Diagnostics, config, exports (non-Godot)

| Feature | Present | Partial | Doc-only | Module | CLI command | Data | Exposable |
|---|:-:|:-:|:-:|---|---|---|:-:|
| `/health` deep check (db, schema, model provider, event channel) | | | ❌ | — | — | — | **Does not exist in any form.** Zero health-check code anywhere (`grep` for `health`/`diagnos` across the whole package returns nothing outside this audit). Phase UI-2 builds this from scratch. |
| `/capabilities` endpoint | | | ❌ | — | — | — | Does not exist. Also built from scratch in Phase UI-2 — this is the mechanism the whole "reality first" guarantee runs on, so it gets its own careful design (§below). |
| SQLite-only persistence; PostgreSQL/pgvector | ✅ (SQLite) | | ❌ (PG) | `db.py` | — | `data/field_horizon.sqlite3` | FABLE §8.5's "prévoir une abstraction propre pour PostgreSQL/pgvector" — **no abstraction layer exists at all.** `config.py`/`db.py` are SQLite-specific throughout (raw `sqlite3.connect`, `PRAGMA` calls). Do not imply a PG option anywhere in Settings; this is future work, not a toggle. |
| Config surface (`config.yaml`) | ✅ | | | `config.py:AppConfig/50`, `load_config/86` | — | `config.yaml` | Yes — full field list audited: paths, Ollama connection/sampling params, retrieval weights, retrieval policy, tone/mode strings. No secrets in this file. |
| Markdown codex export | ✅ | | | `export.py:export_codex/7` | `export-codex` | `outputs/codex.md` | Yes |
| Rust pressure-core export (with graceful degradation if the binary is absent) | ✅ | | | `rustcore.py` | `rust-pressure[-all]` | — | Yes, with the "degraded" state already built in |
| Reproducible cycle export bundle (config, query, ids, model, evidence, scores, events, result, evaluation, provenance, versions — FABLE §18) | | | ❌ | — | — | — | Does not exist as a single bundle. The *pieces* all exist individually (cycle row, `cycle_sources`, `domain_events` by `run_id`, `run_manifests`, provenance report) — Phase UI-6's export work assembles a real bundle from real pieces, doesn't invent new data. |

---

## Architecture note

**Serving `/ui` from the existing server.** `server.py:create_app` (line 101) already builds one `FastAPI` instance with CORS locked to a localhost-only regex (`_LOCALHOST_ORIGIN_REGEX`, line 52) and a single bearer token loaded from a file (`load_or_create_token`, line 55). Adding the web target means: mount `apps/desktop/dist` as a `StaticFiles` directory at `/ui` on this *same* `app` object (not a second FastAPI instance, not a second port) — a one-line `app.mount("/ui", StaticFiles(directory=..., html=True), name="ui")` addition alongside the existing routes. The bearer-token dependency (`require_token`, line 118) already gates every JSON route; the static mount itself should stay unauthenticated (it's just the compiled bundle), with the SPA's own JS performing the token handshake against the JSON API on load — exactly the "paste-token screen reading the token file path shown by the CLI" flow Brief IV's own Phase UI-3 item 2 specifies.

**SSE as a projection of Phase A, not a new system.** Phase A's `domain_events` table and `EventRepository` (`events.py`) are complete and already the single source of truth for cycle/council/dream/retrieval-plan activity. `GET /events/stream` (Phase UI-2 item 3) is a thin `EventSourceResponse` wrapper: poll `EventRepository.recent()`/`by_correlation()`/`by_run()` on an interval (this is a single-user local SQLite database — polling every few hundred milliseconds is not a real cost) or, if worth the complexity later, tail the table via `rowid` cursor; convert each `DomainEvent` row directly into the envelope shape (`event_id`, `run_id`→`correlation_id`, `cycle_id`→`aggregate_id` when `aggregate_type="cycle"`, `timestamp`→`occurred_at`, `event_type`, `payload`) — no new event vocabulary, no second table. **Phase A is fully merged** (confirmed above), so there is no "if Phase A is not yet merged" fallback to design.

**`/capabilities` design.** Given the audit above, this endpoint's job is to report, per capability area, one of `present` / `partial` / `absent`, plus enough detail for the UI to gate correctly: e.g. `{"retrieval_planner": {"status": "present", "strategies": [...]}, "planner_shadow_mode": {"status": "absent"}, "contradictions": {"status": "partial", "detail": "ontological pressure edges only, not persisted, no severity typology"}, "postgres": {"status": "absent"}, "principal_context": {"status": "present", "exposed_via_api": false}}`. It should be computed by introspecting real state where possible (does `provenance_edges` have rows? does the rust binary exist at `rustcore.rust_binary_path`? is Ollama reachable?) rather than a hand-maintained static list that drifts from reality — the whole "no fake facade" guarantee depends on this endpoint staying honest automatically, not on someone remembering to update a dict when a feature ships.

**Tauri managed-backend vs. attach-to-running (FABLE §8.4).** Both flows are needed and neither exists today: "attach" means the Tauri shell just points its `fetch`/EventSource calls at `http://127.0.0.1:<port>` where a `field-horizon serve` process the user started separately is already listening (detected via `GET /health`, once it exists); "managed" means Tauri spawns `field-horizon serve` itself as a child process (a Rust `Command` in `src-tauri`, not a JS `child_process` — Tauri 2's sidecar mechanism), tracks its PID, and is the only thing allowed to kill it. `run_server` (`server.py:324`) already takes `port`/`token_file` as plain arguments, so the managed-backend path is "spawn this existing CLI entry point as a subprocess," not new backend code.

**Browser token flow.** The token file (`load_or_create_token`, written with `chmod(0o600)`) already exists for the desktop/CLI case. For the web target: the CLI prints the token file's path (a small addition to `serve`'s startup output), the user opens `/ui`, a first-run screen asks them to paste the token (read from that file path themselves, e.g. `cat .fh_token`) or upload the token file directly via a `<input type=file>` (never a text field that could get logged/autofilled insecurely) — the token then lives only in memory / `sessionStorage`, never bundled, never in a JS source file.

## Risks and open questions

1. **Planner mode UI (FABLE §10.5) has no backend to back it.** Confirmed: `legacy`/`shadow`/`active` do not exist as distinct modes — there is one `plan_and_retrieve` function with an opt-in flag. Recommend: build a single RETRIEVAL screen with a `--plan` toggle and skip the three-mode comparison UI entirely for now; document this explicitly in `UI_FEATURE_COVERAGE.md` as "planner comparison: not applicable, no shadow-mode execution path exists" rather than attempting to fake a second execution path.
2. **CONTRADICTIONS severity typology (FABLE §10.11) doesn't exist.** The screen can only honestly show: axiom A, axiom B, one pressure score, one reason string. Recommend building exactly that (a simple two-column comparison + score), explicitly labeled as "ontological pressure, not a general contradiction detector," rather than inventing categories (tension/ambiguity/temporal-divergence/etc.) the backend has no way to populate.
3. **Reproducible export bundle (FABLE §18) requires assembling five-plus real sources per cycle** (cycle row, cycle_sources, domain_events by run_id, run_manifests, provenance report) that have never been joined into one payload before. This is real, buildable work, not a gap — flagging it because it's more backend work than "wrap an existing endpoint," and belongs in Phase UI-2's application-service consolidation, not deferred to UI-6's export phase as pure frontend work.
4. **`/health`'s "model provider" check needs a live Ollama call**, which is the one dependency this whole system has ever treated as best-effort/degradable (see `registries.py`'s digest-resolution fallback). The health check must report "degraded, not failed" when Ollama is unreachable but the DB/schema/event channel are fine — matching this codebase's own long-standing "no silent degradation, but degrade explicitly rather than crash" discipline.
5. **Should the SSE endpoint poll or use a DB-level notification mechanism?** SQLite has no native LISTEN/NOTIFY. Polling `domain_events` by `id > last_seen` on a short interval is simple, correct, and cheap at this system's scale (single user, local disk) — flagging only because "SSE" sounds like it implies push, and the honest implementation is a fast poll dressed as a stream. Worth confirming this is an acceptable tradeoff before Phase UI-2 builds it.
6. **Dream/proposal `apply` performs a real git commit against `ontology.yaml`** (`proposals.py:_run_ontology_parity_check` + the commit call inside `apply_proposal`). The DREAMS screen's "apply" action is therefore not a simple state change — it's a destructive-adjacent, repo-mutating action requiring the explicit-confirmation treatment Brief IV's own Phase UI-5 item 4 already calls for. Noting it here because it's the single most consequential button in the entire application, and it needs its own review rather than reusing the app's generic "destructive action" confirmation copy.
7. **No open question blocks starting Phase UI-2** — the gaps above are all things Phase UI-2/UI-4/UI-5 already exist to build, not missing information that would change the plan.
