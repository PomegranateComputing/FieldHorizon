# Field Horizon v3.2 — Architecture Review

**Reviewed:** full project dump of 2026-07-09 (Python package `fieldhorizon`, corpus data, Rust `fh-core` scaffolding, config).
**Verdict in one sentence:** the design is genuinely original and worth keeping — a recursive doctrine engine with adversarial agents, an ontological pressure graph, and a canon/heresy taxonomy — but a single bug currently prevents the recursion from ever happening, and the ontology that makes the project weird is scattered across five hardcoded dictionaries instead of being the engine's first-class data model.

---

## 1. The critical bug: nothing can ever become CANON

`evaluate.py`, `has_truncation_marker()`:

```python
if re.search(r'[,;:]*$', stripped):
    return True
```

`[,;:]*` matches *zero or more* characters, and `$` anchors at end-of-string, so `re.search` finds an empty match on **every possible input**. Verified: the function returns `True` unconditionally for any non-empty fragment.

The cascade this causes:

1. Every fragment receives `t_penalty = 0.35`.
2. The maximum achievable `final_score` is `1.0 − 0.35 = 0.65`, which is below the `USEFUL_FRAGMENT` threshold of `0.68` and far below the `CANON` threshold of `0.82`.
3. The hard gate `t_penalty <= 0` in `canon_allowed` can never pass.
4. Therefore **every cycle in the system's history has been classified HERESY or NOISE**, `outputs/canon/` never fills, `load_canon_fragments()` always returns an empty list, and the recursive-canon feedback loop — the soul of the project — has never executed.
5. Side effect: `should_rewrite()` fires on every single cycle, so you pay a critic pass plus a rewrite pass on every generation, and the rewrite can never rescue the score either. The system is burning roughly 3× the LLM calls it should, to produce output it then permanently disqualifies.

The intended check was presumably "ends with a dangling comma/semicolon/colon":

```python
if re.search(r'[,;:]$', stripped):   # + not *
    return True
```

While you're in there, `stripped.endswith("...")` also flags legitimate rhetorical ellipses, and the `len(last) <= 3` heuristic flags fragments ending in short words followed by an em-dash or closing bracket variants you didn't enumerate (`»`, `’`). A safer shape for the whole function: check a small set of *positive* completion signals (ends with sentence-terminal punctuation, balanced quotes/brackets, minimum length) and treat everything else as suspect, rather than enumerating failure patterns. Enumerated-failure heuristics are exactly how this bug slipped in.

There is a second, quieter instance of the same disease: `multicycle.py` has its own `fragment_looks_truncated()` with *different* rules than `evaluate.has_truncation_marker()`. A fragment can pass the multicycle retry gate and then be executed by the evaluator for the same offense. One truncation detector, one module, imported by both.

## 2. The second critical finding: the Rust core almost certainly never runs

`rustcore.rust_binary_path()` returns the hardcoded relative path `fieldhorizon-rust/target/release/fh-core`. Two problems:

1. Your project tree shows only `target/debug/` was ever built. No release binary exists, so `run_rust_pressure_core` raises `FileNotFoundError` on every call.
2. `multicycle.run_multi_cycle` wraps the call in `try/except Exception` and silently substitutes `"[rust pressure unavailable: ...]"` into the synthesis prompt. Silent degradation means you have likely been running multi-cycles for the project's entire life believing the pressure graph was informing synthesis when it never was.

Combined with finding 1, the honest state of the system is: no canon, no pressure graph, every verdict HERESY — a machine that judges everything it makes as heresy and forgets everything it learns. That is thematically perfect and operationally useless.

Fixes: resolve the binary path relative to `cfg.root` (which itself should be derived from the config file's location, not `Path.cwd()` — see §5); fall back to `target/debug/fh-core` if release is absent; and log the degradation loudly (a `logging.warning`, and a line in the cycle's markdown log) instead of burying it in prompt text an 8B model will ignore.

Also note the dump itself: your review-dump script included 15,000 lines of the quarantined Quran text and the entire `target/` fingerprint tree, while **omitting `fieldhorizon-rust/src/*.rs` entirely** — I could not review `pressure.rs`, `types.rs`, `io.rs`, or `main.rs` because they are not in the file. The dump filter is inverted for the Rust tree: it kept the build noise and dropped the source. Make the dump script honor `.gitignore` and explicitly include `src/`.

## 3. What is genuinely good (keep all of it)

The four-agent adversarial structure in `agents.py` with explicitly assigned enemies and a prohibition on consensus is a real idea, not decoration — it forces the synthesizer to resolve tension rather than average four essays. The CANON/USEFUL/HERESY/NOISE taxonomy with hard gates is a better quality-control vocabulary than any generic "score ≥ threshold" pipeline. The `canon_loop_penalty` list ("fans whir", "skulls", "officiant") is empirical evidence you already watched a feedback loop collapse into motif inbreeding and built an immune response — that instinct is exactly right, it just needs generalizing (§7). The distillation pattern in the culture-war corpus — raw polemic in `gloss`, abstracted sociological claim in `statement`, provenance preserved — is a defensible and honestly interesting design: the system metabolizes rage into analyzable doctrine rather than repeating it. Keep it that way: this project is sound as a closed, reflective instrument that studies how polemic hardens into doctrine; it would become a very different and much less defensible thing if its outputs were ever wired to distribution channels, and nothing in the current architecture does that.

The Gödel-engine corpus mapping incompleteness theorems onto theological exile is the best writing in the dataset, and notably it's also the only corpus with *varied, meaningful* severity/mutation metadata — which brings us to what's wrong with the rest.

## 4. The ontology is the product, so stop hardcoding it

The conceptual machinery that makes Field Horizon weird currently lives in **five separate hardcoded Python dictionaries across three files**, plus presumably a sixth copy inside the Rust core:

`DOMAIN_HINTS` and `SOURCE_EXPANSIONS` and `domain_expansions` in `retrieval.py`; `AXIOM_KEYWORDS` and `normalize_ref`'s enumerated prefixes in `evaluate.py`; `OPPOSITION_PAIRS` in `ontology.py`; the category-specific directives baked into `REWRITE_PROMPT` in `rewrite.py`.

Consequence: adding one new tradition (say, a `kabbalah.json` corpus) requires editing four Python files and one Rust file in perfect sync, and `normalize_ref` will silently mis-score anything you forget. This is the single largest architectural flaw after the two bugs above, because it makes the system's defining feature — an extensible symbolic ontology — the hardest thing to extend.

The industry-standard move that *increases* the weirdness rather than sanding it off: one declarative `ontology.yaml` as the source of truth, loaded by both Python and Rust:

```yaml
domains:
  tawhid:
    hints: [tawhid, unity, idol, idolatry, shirk, allah, transcendence]
    expansions: [worship, lord, one, god]
    sources: [sacred_quran, red_flags, sacred_bible]
    rewrite_directive: "develop unity, idolatry, worship, false mediation, transcendence"
  godel_engine:
    hints: [godel, incompleteness, undecidable, self_reference, consistency]
    ...
oppositions:
  - [tawhid, multiplicity, "unity versus fragmentation"]
  - [logos, bureaucracy, "meaning versus administration"]
  ...
```

Then a new tradition is a YAML block plus a JSON corpus file, no code edits, and the Rust and Python pressure engines can never disagree because neither owns the pairs. Generate the rewrite directives and the evaluator keywords from the same block. This one refactor deletes perhaps 300 lines of brittle duplication and turns the ontology into user-facing surface area — which is the project's actual value proposition.

## 5. Correctness and code-quality defects, in priority order

**The FTS5 tables are dead weight and slowly corrupting.** `db.py` creates `chunks_fts` and `json_entries_fts` as external-content tables (`content=''`), ingest writes into them — and no query in the codebase ever reads them. `retrieval.py` does `LIKE '%term%'` scans on `chunks` and loads the **entire** `json_entries` table into Python to sort it. Worse, `ingest_books` deletes and re-inserts sources/chunks on re-ingest but never deletes the corresponding FTS rows, so `chunks_fts` accumulates ghost entries forever. Decide: either actually query FTS5 (`MATCH` with BM25 ranking — you get relevance scoring for free and can delete `row_score`'s regex loop), or drop the tables. At current corpus size (~hundreds of chunks) the LIKE scans are survivable; the half-maintained FTS is not, because it's a data-integrity time bomb the moment you do use it.

**`--auto-rewrite` cannot be turned off.** In the `multi-cycle` subparser: `action="store_true", default=True`. Passing the flag sets it to `True`; not passing it leaves it `True`. Use `argparse.BooleanOptionalAction` (gives you `--no-auto-rewrite`) or a `--skip-rewrite` inverse flag.

**Metadata generation is inconsistent between the two pipelines, which makes the evaluator scores incomparable.** `multicycle.py` correctly builds `METADATA_JSON` in code (`build_structured_response`), so `source_alignment` and `json_alignment` are trivially 1.0 there — the metrics measure nothing. `cycle.py` instead demands an 8B model at temperature 1.25 echo reference strings verbatim, then scores it on echo fidelity — measuring instruction-following, not doctrinal quality, and `normalize_ref`'s substring matching (`ref_norm in normalized_meta`) can false-positive when one ref is a prefix of another. Adopt the multicycle approach everywhere: the model writes prose, the code writes metadata. Then delete `source_alignment`/`json_alignment`/`metadata_validity` from the score and re-weight the components that measure the actual text.

**Duplicated logic with divergent behavior.** `extract_field_fragment` exists in both `canon.py` (regex-based, anchored to `## Response`, which no current log format emits — check whether it ever matches your v3 logs) and `multicycle.py` (split-based). `should_rewrite` and `canon_dir_for_verdict` are copy-pasted between `cycle.py` and `multicycle.py`. Truncation detection exists twice (§1). Create a `fragments.py` module owning extraction/validation, and a `verdicts.py` owning routing; import everywhere.

**The markdown logs are the real database and the SQLite database is a write-only log — invert this.** Canon fragments are recovered by regexing `.md` files whose format is defined by string concatenation in two different functions; one formatting tweak silently breaks `extract_field_fragment` and the canon disappears (again). You already have a `cycles` table storing the final response. Make the DB the source of truth: add `verdict`, `final_score`, and an extracted `fragment` column to `cycles`; make `load_canon_fragments` a `SELECT ... WHERE verdict='CANON' ORDER BY created_at DESC`; keep the `.md` files as generated views for reading. `build_cycle_index` then becomes a query instead of a directory crawl.

**Config root is `Path.cwd()`.** `load_config` resolves everything relative to the current working directory, and `rust_binary_path` compounds it. Run the CLI from anywhere but the repo root and every path breaks. Resolve `root` from the config file's own location: `root = cfg_path.resolve().parent`. Also, `manifestos` is read from the raw top level of the YAML while every other path lives under `paths:` — and the shipped `config.yaml` doesn't define it at all, so it silently defaults. Move it under `paths:`.

**`export_rust_pressure_report_all` runs on every multi-cycle.** It re-merges the entire JSON corpus to disk, spawns the binary, and rewrites the report file, per query. Cache it keyed on a hash of the corpus directory's mtimes/contents; regenerate only on change.

**Ollama client robustness.** `call_ollama` has no retry, no backoff, one fixed 300 s timeout, a fresh TCP connection per call (`requests.post` without a `Session`), and returns `""` on a malformed response, which then flows through the whole pipeline as an empty fragment. Use a module-level `requests.Session`, retry transient failures (connection errors, 5xx) with jittered backoff, and raise on empty content so the retry loop in multicycle triggers for the right reason. Replace the bare `print(f"[WARN] ...")` in `ingest.py` with the `logging` module; you have Rich installed — `rich.logging.RichHandler` gives you severity-colored logs for free, which suits the aesthetic.

**Packaging and hygiene.** No `pyproject.toml`, no lockfile, no tests, no CI in the dump. The evaluator is pure functions over strings — it is *ideally* testable, and §1 is precisely the bug a five-line unit test would have caught the day it was written (`assert not has_truncation_marker("A complete sentence.")`). Minimum bar: `pyproject.toml` with pinned deps, `pytest` with tests for `evaluate.py`, `retrieval.terms/detect_domains`, `interpreter.extract_json_object` (which, credit where due, is a correctly written brace-balancing parser with string-escape handling), `ruff` + `mypy` in pre-commit, and a GitHub Actions job running them. Add `.gitignore` entries for `target/`, `outputs/`, `logs/`, `*.sqlite3`.

**O(N²) pressure graph.** `build_pressure_edges` iterates `combinations(nodes, 2)` — 1,000 axioms is half a million pairs, 10,000 is fifty million. Since edges only exist between categories present in `OPPOSITION_PAIRS`, group nodes by category first and only cross the opposing groups. Same fix belongs in the Rust core, which at current corpus size is honestly premature optimization — its only justification is being the *single* pressure engine (per §4), not a faster duplicate of a Python one.

## 6. The self-evolution loop is currently sterile

`ontology.candidate_statement` maps each opposition reason to one of seven **fixed template strings**. The pipeline is: same corpus → same opposition pairs → same seven sentences → `dedupe_candidates` collapses them → `generated_axioms.json` is overwritten with essentially the same ≤7 axioms plus the generic fallback sentence, forever. The "doctrinal mutation" advertised by `mutation_potential` never mutates anything. Meanwhile every generated axiom hardcodes `tone: severe, category: recursive_axiom` — flat metadata, like the culture-war corpus, where every single entry is `severity: 0.72, mutation_potential: 0.66` with identical targets. Flat metadata means `json_score`'s `severity * 2` term adds a constant to everything: dead signal.

To make the loop actually evolve: feed each high-pressure edge's two *actual statements* (not just category names) to the LLM with a constrained prompt ("produce one axiom that neither side could accept but both imply"), run the result through the critic with a dedicated verdict, and only promote survivors into `generated_axioms.json` — with severity/mutation estimated per-axiom by the critic rather than templated. Version the file instead of overwriting (`generated_axioms_0007.json`), so canon has geological strata. That gives you real mutation with a quality gate, which is both the industry-standard pattern (generator–verifier) and far weirder in output than seven templates.

Same treatment for the distiller that produced `culture_war.json`: have it estimate severity per item and derive targets from the item, not the batch. Provenance in `gloss` is good — also add a `source_file` and `source_line` field so quarantine decisions are auditable.

## 7. Fixing the canon feedback loop before you turn it on

Once §1 is fixed, canon will start accumulating and `load_canon_fragments(limit=3)` will inject the **three most recent** canon fragments into every prompt. That is a recipe for the motif collapse you already experienced — recency-based selection means the newest tic dominates all future generations within days. `canon_loop_penalty`'s hardcoded phrase list is a tombstone of the last collapse, not a defense against the next one, since the next loop will fixate on phrases you haven't listed yet.

Robust version, still fully local: embed every canon fragment at write time (Ollama serves embedding models, e.g. `nomic-embed-text`; store vectors in a `canon_embeddings` table or `sqlite-vec`). At retrieval, select canon by *maximal marginal relevance* — relevant to the query but mutually dissimilar — and track per-motif usage counts, decaying the sampling weight of any n-gram that appears in more than k recent canon entries. Replace the hardcoded `canon_loop_penalty` phrases with a statistical check: penalize any 3-gram whose frequency across the last N canon fragments exceeds a threshold. Now the immune system generalizes.

The same embedding infrastructure upgrades retrieval itself: hybrid search (embedding similarity + your `DOMAIN_HINTS` routing as a bias term, + FTS5 BM25 if you keep it) replaces the `LIKE '%term%'` scans. Crucially, keep the domain routing — steering "technology" queries toward Quranic apocalypse chunks is an editorial stance, and it's the interesting part. Embeddings should widen recall underneath that stance, not replace it.

## 8. Generation-quality issues

Temperature 1.25 combined with a strict output contract ("copy these refs exactly, exact section labels, 250–500 words") is self-sabotage: you demand maximal entropy and perfect compliance simultaneously, which is why the codebase has grown three retry attempts, a critic, a rewriter, and truncation detectors. `multicycle` already discovered the right pattern — prose-only generation with metadata assembled in code. Complete the thought: high-temperature pass for prose, and if you ever need model-produced structure, a second temperature-0 pass. Then most of the retry scaffolding can shrink.

The evaluator's remaining text metrics (`symbolic_density` over a fixed 21-word list, `doctrinal_enforcement` via `AXIOM_KEYWORDS`) are Goodhart bait — the rewriter's prompt literally instructs the model to use "exact category terms or close lexical forms," i.e., you have already taught the generator to stuff the evaluator's keywords. Once embeddings exist (§7), score doctrinal enforcement as cosine similarity between each retrieved axiom's statement and the fragment's sentences (max over sentences, mean over axioms). Keep the keyword lists only as a cheap pre-filter. And the word-count check `if words < 180` in `evaluate` disagrees with `fragment_is_usable`'s `>= 180` and the prompt's "250 to 500" — pick one contract, define it once.

`clip()` in `prompting.py` appends `" [...]"` to truncated retrieval chunks — and `has_truncation_marker` treats `[...]` anywhere in a fragment as truncation. If a model ever echoes a clipped source, it gets executed for your own ellipsis. Use a marker the evaluator ignores, e.g. `⟨cut⟩`.

## 9. Prioritized roadmap

**Now (hours):** fix the `[,;:]*` regex and add the unit test that would have caught it; fix `rust_binary_path` (release→debug fallback, root-relative) or build the release binary; make Rust-pressure failure loud; fix `--auto-rewrite`; then re-run a batch of cycles and watch `outputs/canon/` fill for the first time in the project's life.

**Next (a weekend):** unify fragment extraction/truncation/verdict-routing into shared modules; code-generated metadata in both pipelines and re-weighted scoring; DB as source of truth for canon with `.md` as views; config root from file location; `pyproject.toml` + pytest + ruff + CI; logging module; requests Session with retries; dump script that excludes `target/`/quarantine and includes Rust `src/`.

**Then (the real upgrade):** `ontology.yaml` as the single declarative ontology consumed by Python and Rust; local embeddings for hybrid retrieval, MMR canon selection, statistical motif-loop detection, and embedding-based doctrinal enforcement; LLM-in-the-loop axiom mutation with critic gating and versioned strata; per-item metadata from the distiller.

**Optional flourishes that fit the project's nature:** a `field-horizon lineage <axiom_id>` command that walks provenance from a generated axiom back through pressure edges to raw sentences and book chunks — the system's whole thesis made inspectable; a Rich-rendered live view of a multi-cycle (four agent panels arguing, then synthesis); and a periodic "council" cycle where the critic re-tries old HERESY fragments against the grown canon — doctrine rehabilitating its heretics is both a real re-ranking mechanism and exactly the kind of weird this codebase deserves.
