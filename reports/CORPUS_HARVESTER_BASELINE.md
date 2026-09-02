# Field Horizon — Autonomous Open Corpus Harvester
## Phase 0 — Repository Baseline Audit

Generated before any code for this mission was written.

---

## 1. Starting point

| Item | Value |
|---|---|
| Starting commit | `168413024a2333ce16a9916500b21425eb8a6ef9` |
| Starting branch | `main` |
| Working branch created | `feature/autonomous-open-corpus-harvester` |
| `git status` at start | **clean** — no untracked, staged, or modified files |
| Python | 3.14.4 (`.venv`) |
| Project version | `fieldhorizon 3.2.0` (editable install) |
| Baseline test suite | **635 passed, 0 failed, 1 warning — 84.68s** |

Because the tree was clean at start, every file this mission adds or
modifies is unambiguously attributable to it. No pre-existing user
modification exists to protect, preserve, or exclude from commits.

---

## 2. Detected architecture

Field Horizon is a **closed, local, reflective doctrine-synthesis engine**.
It has no acquisition layer at all today: the corpus is whatever a human
placed on disk and listed in a curation manifest.

### 2.1 Package layout

Single Python package `fieldhorizon/` (~16.6k lines across 51 modules),
plus a Rust sidecar (`fieldhorizon-rust/`) and a Tauri desktop GUI
(`apps/`). Relevant modules:

| Module | Role |
|---|---|
| `config.py` | `AppConfig` frozen dataclass loaded from `config.yaml`; **all paths resolved relative to the config file's own directory**, never cwd |
| `db.py` | Whole SQLite schema as one `SCHEMA` string + guarded `ALTER TABLE` migration functions run by `init_db()`; `PRAGMA user_version` = `CURRENT_SCHEMA_VERSION` (currently **4**) |
| `ingest.py` | `chunk_text()`, `ingest_books()`, `ingest_manifestos()`, `ingest_json_corpus()`, `ingest_from_manifest()` |
| `manifest.py` | `data/sources.yaml` loader — curation-as-code, validated, idempotent by `manifest_id` |
| `retrieval.py` | FTS5 + embedding + hybrid v2 retrieval over `chunks` |
| `llm.py` | `call_ollama()` / `embed()` — local Ollama only, retry with jittered backoff, `OllamaError` |
| `events.py` | `domain_events` append-only fabric + `OperationEmitter` |
| `provenance.py` | Typed provenance edges over existing rows (6 object types) |
| `cli.py` | ~40 argparse subcommands, one `build_parser()` |
| `diagnostics.py` | `compute_capabilities()` / `compute_health()` |

### 2.2 Corpus storage as it exists

```
data/
  books/           (empty except .gitkeep — gitignored: data/books/*)
  manifestos/      test_manifesto.txt  (gitignored)
  json_corpus/     11 JSON axiom files
  sources.yaml     curation manifest, 6 entries
  field_horizon.sqlite3
```

**Critical naming finding.** The mission brief says `data/manifesto`. The
repository uses **`data/manifestos`** (plural), configured as
`paths.manifestos` in `config.yaml` and exposed as `AppConfig.manifestos`.
The harvester adapts to the repository's real convention. Likewise the
`source_type` string used in the DB and in retrieval routing is the
**singular** `"manifesto"` — those are two different names for two
different things and both are preserved exactly as they are.

**Second finding.** `data/books/` is empty on this machine. `bible.txt`,
`quran.txt`, and `redflags.txt` are referenced by `data/sources.yaml` but
are not present locally (they are gitignored user corpora). They
therefore cannot be, and will not be, modified by this mission.
`data/manifestos/*.txt` do exist and are likewise untouched.

### 2.3 Database schema (pre-existing, 22 tables)

`sources`, `chunks`, `chunks_fts` (FTS5), `json_entries`,
`json_entries_fts`, `cycles`, `cycle_sources`, `canon_events`, `councils`,
`canon_embeddings`, `canon_ngrams`, `axiom_embeddings`,
`chunk_embeddings`, `chunk_concepts`, `chunk_entities`, `chunk_motifs`,
`source_fingerprints`, `weather_axis_vectors`, `weather_readings`,
`schools`, `school_members`, `domain_events`, `run_manifests`,
`model_registry`, `prompt_registry`, `embedding_registry`,
`provenance_edges`, `canon_temporal_states`.

`sources` already carries `manifest_id`, `author`, `year`, `license`,
`weight`, `notes`, `language` — a real provenance surface the harvester
can populate rather than duplicate.

### 2.4 Migration convention

There is **no migration framework**. The established pattern is:

1. Append `CREATE TABLE IF NOT EXISTS` to `db.SCHEMA`.
2. Add a `_migrate_*` function using `PRAGMA table_info` guards for any
   column added to a table that already shipped.
3. Call it from `init_db()`.
4. Bump `CURRENT_SCHEMA_VERSION`.

The harvester follows this exactly — additive only, no destructive
rewrites, safe to re-run.

---

## 3. Integration points chosen

| Need | Existing mechanism reused | Why |
|---|---|---|
| Config | `config.yaml` + `AppConfig` | Harvester config is two new YAML files read through the same root-relative resolution; `AppConfig` is extended with a defaulted field so the dozens of tests that build `AppConfig` directly keep working |
| Persistence | same SQLite DB, `db.connect()` | Rule: "éviter deux systèmes concurrents" |
| Migrations | `init_db()` + `_migrate_*` | Only established pattern |
| Ingestion | `sources` / `chunks` / `chunks_fts` rows written the same shape `_ingest_manifest_entry` writes | Retrieval, hybrid scoring, embeddings, concepts, and fingerprints then work on harvested documents with **zero** changes to those subsystems |
| Idempotence | `sources.manifest_id` partial unique index | Already exists and already means "ingest this once, by id, not by path" |
| LLM | `llm.call_ollama` | Local Ollama, no commercial API — matches rules 17/18 |
| JSON from LLM | `interpreter.extract_json_object` | Already tolerant of fenced/dirty model output |
| Events | `events.OperationEmitter` | Harvester runs become watchable on the existing `/events/stream` |
| Logging | stdlib `logging` per module | Repo-wide convention |
| CLI | `cli.build_parser()` | One `corpus` subparser group, mounted from `corpus/cli.py` |

### 3.1 The one modification to an existing behaviour

`llm.call_ollama()` hardcodes `prompting.SYSTEM_PROMPT` — the Field
Horizon doctrinal persona ("dense theological, metaphysical, apocalyptic
prose", "do not output structured data"). That prompt is actively
hostile to a JSON-returning classifier, and it grants no
prompt-injection defence.

An **optional, defaulted** `system=` parameter is added to
`call_ollama()`. Every existing call site is unaffected (default `None`
→ `SYSTEM_PROMPT`, byte-identical behaviour). The classifier passes a
neutral, injection-hardened system prompt instead. This is the minimum
change that makes rule 12–14 (downloaded text is data, never
instruction) enforceable at the model boundary.

---

## 4. Dependency decision

Installed: `requests`, `PyYAML`, `rich`, `numpy`, `fastapi`, `uvicorn`,
`pydantic` (+ dev: `pytest`, `ruff`, `mypy`, `httpx`). **Not** installed:
`lxml`, `beautifulsoup4`, `defusedxml`, `ebooklib`, `pypdf`,
`langdetect`, `datasketch`.

**Decision: add zero new dependencies.** Rationale, per the brief's
"réutilise les bibliothèques déjà présentes" and "ne pas introduire un
framework massif pour une tâche simple":

| Need | Chosen implementation | Justification |
|---|---|---|
| XML (OPDS, OAI-PMH, RDF, SRU) | `xml.etree.ElementTree` behind a hardened wrapper that rejects DOCTYPE/ENTITY declarations *before* parsing and caps input size | Kills XXE and billion-laughs deterministically and testably; `defusedxml` would add a dependency to do the same thing |
| HTML | stdlib `html.parser.HTMLParser` subclass that drops `script`/`style`/`nav` subtrees and never executes anything | Rule 13/"aucun HTML rendu activement". `lxml`/`bs4` would be heavier and no safer here |
| EPUB | stdlib `zipfile` + the hardened XML/HTML parsers, walking `container.xml` → OPF → spine | EPUB *is* a zip of XHTML; `ebooklib` would add a dependency for path resolution we need to zip-slip-harden ourselves regardless |
| PDF | Minimal in-repo text-layer extractor (`FlateDecode` via stdlib `zlib`, `Tj/TJ/'/"` operators). Deliberately weak: it **measures** its own extraction quality and rejects to `QUALITY_REJECTED` when the text layer is poor | PDF is last in the format preference order and non-PDF is always preferred. A weak-but-honest extractor that reports its own failure beats a heavy dependency for a format we avoid. Documented as a known limitation |
| Near-duplicate detection | SimHash (64-bit, `blake2b` token hashing) + shingle MinHash, in-repo | ~120 lines of exact, testable arithmetic; `datasketch` is a whole framework for it |
| Language detection | Deterministic stopword+trigram profile scorer for fr/en/de/es/it/la (+ `unknown`) | Offline, seed-free, reproducible — mandatory for rule 20; `langdetect` is nondeterministic without a fixed seed |
| HTTP | `requests` (already pinned at 2.34.2) | Already a dependency |

No SaaS is used for anything. `numpy` is available but the harvester core
does not require it.

---

## 5. Regression risks and how each is contained

| Risk | Containment |
|---|---|
| Harvested rows pollute existing retrieval results | Harvested sources get their own `source_type` values (`book` / `manifesto` — the same two the engine already routes) but carry `manifest_id` prefixed `corpus:`; every corpus row is separable by that prefix, and `sources.weight` is set from selection confidence so a harvested text never automatically outranks a curated one |
| Schema change breaks an existing test | Purely additive tables + one defaulted `AppConfig` field. `CURRENT_SCHEMA_VERSION` bumped 4 → 5. All 635 existing tests must still pass — this is a hard gate on the mission |
| `_delete_stale_fts_for_source` semantics | Corpus ingestion reuses the same delete-then-insert discipline for `chunks_fts`, or a harvested re-ingest would accumulate ghost FTS rows |
| Existing files modified | The harvester writes **only** into `data/corpus/**`, `data/books/_auto/**`, `data/manifestos/_auto/**`, `outputs/corpus/**`, `logs/corpus/**`. It never writes `data/sources.yaml`, never touches `bible.txt` / `quran.txt` / `redflags.txt` |
| Network in the unit suite | Every adapter takes an injected fetcher; the offline fixtures never construct a real HTTP client. A guard test asserts the unit suite performs no socket connection |
| Disk exhaustion | `min_free_disk_bytes` + `max_total_corpus_bytes` are refused-to-run-without, checked before every download |
| Two concurrent syncs | `data/corpus/locks/` PID lockfile with staleness detection |

---

## 6. Deviations from the brief, and why

Every deviation is a repository-reality adaptation, not a scope
reduction.

1. **`data/manifesto` → `data/manifestos`.** The brief's path does not
   exist. The repo's `paths.manifestos` does. Materialization targets
   `data/manifestos/_auto/`.
2. **`fieldhorizon/corpus/` module list.** Implemented as specified, plus
   `schema.py`, `repository.py`, `policy.py`, `language.py`, `http.py`,
   `ingestion.py`, and `cli.py` — separating DB access, config loading,
   and CLI mounting from the logic modules follows this repo's own
   one-concern-per-module layout.
3. **`registry.py`** lives at `corpus/adapters/registry.py` (it registers
   adapters; nothing else registers anything).
4. **Wikisource** is added as `adapters/wikisource.py` in addition to the
   brief's `adapters/wikimedia.py` naming — `wikimedia.py` holds the
   shared MediaWiki API/dump machinery, `wikisource.py` the source
   definition, because Wikimedia dumps are the reusable brick.
5. **CLI is `field-horizon corpus <verb>`**, not `fieldhorizon corpus
   <verb>` — the installed console script is `field-horizon`.
6. **PDF and OCR.** OCR is not implemented at all (disabled by default in
   the brief; no OCR engine is installed and adding one is out of
   proportion). The config key exists and is documented as unimplemented
   rather than silently stubbed.
7. **No `reports/` directory existed.** Created for this mission's two
   report deliverables.

---

## 7. Diagnostics observed at baseline

`field-horizon status` and the `diagnostics` module run cleanly. Ollama
availability is environment-dependent and is *not* assumed: every
harvester path that could use the LLM has a deterministic fallback, and
the classification fallback is exercised by its own tests regardless of
whether a model is reachable.

---

## 8. Definition of the mission's own hard gates

- All 635 pre-existing tests still pass.
- New tests pass with no network access.
- No file outside the harvester's own write-set is modified, except the
  four surgical edits declared in §3.1 and §5 (`llm.py` system param,
  `db.py` schema+migration, `config.py` defaulted field, `cli.py`
  subparser mount, `retrieval.py` provenance enrichment).
- No document reaches the index without a recorded rights decision whose
  `decision == "accept"`.
