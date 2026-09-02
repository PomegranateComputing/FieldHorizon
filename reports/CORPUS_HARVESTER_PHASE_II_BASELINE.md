# Corpus Harvester — Phase II Baseline

## Source graph, rights lattice, and institutional scale

Written before any Phase II code, from an audit of the Phase I
acceptance report, the eleven defects the live pilot found, the corpus
modules, the migrations, the fixtures, the CLI, and the Git state.

---

## 1. Starting point

| Item | Value |
|---|---|
| Parent branch | `feature/autonomous-open-corpus-harvester` |
| Parent commit | `1bc5173645d3238b3b2356f7f8fda9ed3912f1ce` |
| Phase II branch | `feature/autonomous-open-corpus-phase-ii` |
| `git status` | **clean** — no untracked, staged, or modified files |
| Tests | **953 passing** (635 pre-existing + 318 corpus) |
| Corpus modules | 21 modules, 13,903 lines under `fieldhorizon/corpus/` |
| Adapters | 9 registered |
| Offline fixtures | 22 files under `tests/fixtures/corpus/` |
| Schema version | 5 |
| Live corpus | 28 documents indexed, 33.5 MB |

---

## 2. The central defect Phase II must fix

Phase I collapsed four distinct roles into one `SourceConfig`. Every
adapter is assumed to *discover*, *describe*, *prove rights for*, and
*host* its documents. Nearly every limitation in the Phase I acceptance
report is a symptom of that single conflation:

| Phase I symptom | The role that was actually missing |
|---|---|
| DOAB "yields metadata but not text" (OAPEN 403) | `content_hosting` belongs to a different provider than `discovery` |
| Standard Ebooks limited to 15 recent releases (401 on bulk) | `bulk_snapshot` requires credentials the discovery path does not |
| Wikisource harvests nothing (robots.txt) | `bulk_snapshot` via dumps is a *different transport* from `discovery` via API |
| Europeana disabled pending "per-provider host allowlisting" | Europeana is a `discovery` + `rights_evidence` broker; hosting is elsewhere |
| Gallica disabled pending live test | `rendered_text` (text mode / ALTO) is a distinct capability from `metadata` |

In every case the code had to treat the source as **broken** because it
could not do all four things. A source that does two of them well is not
broken; the model was.

**Phase II replaces the assumption with a graph.** `SourceCapabilities`
declares what a provider actually does; `AcquisitionPlan` records which
provider filled each role for a given document.

---

## 3. The second defect: rights are computed from one licence

`rights.evaluate()` takes a single `RightsSignal` with one
`content_license` and adjudicates it. Defects **#1 and #2** of the
eleven — the most consequential the pilot found — were both instances of
this:

> Standard Ebooks states *"Public domain in the United States"* for the
> **work** while dedicating its **edition** under CC0. The CC0 pattern
> matched first and produced a worldwide grant.

Phase I fixed that by **reordering regex patterns** so the jurisdictional
statement is matched before CC0. That fix is correct and is covered by a
test, but it is a fix at the wrong level: it makes one pair of layers
resolve correctly by ordering, rather than modelling the layers.

The `rights._TEXT_PATTERNS` ordering is now load-bearing, and
`CORPUS_RIGHTS_AND_EXPORT.md` carries a standing warning saying so. That
warning is an admission that the model is missing.

**Phase II replaces first-match-wins with a lattice.** Rights are
computed per `RightsComponent` (work, source text, translation,
illustrations, editorial contribution, digital edition, metadata, scan,
OCR) and the effective rights are the **intersection** of the applicable
components. A broader permission on a secondary component can then never
widen the primary content — not by convention, but because intersection
cannot widen anything.

The Standard Ebooks case becomes structural rather than incidental:

```
source_text            = PUBLIC_DOMAIN_US        (scope LOCAL_US_ONLY)
editorial_contribution = CC0_WORLDWIDE           (scope WORLDWIDE)
                       ────────────────────────
effective              = LOCAL_US_ONLY
```

---

## 4. The third defect: paths

Defect **#8** was the worst kind — a test artefact reaching production:

> A cache path resolved against the **working directory**, so the offline
> test suite wrote its fixture catalogue into the real
> `data/corpus/manifests/`, and a live run then labelled a real document
> with a fixture's title.

Phase I fixed the specific path (`SourceConfig.cache_dir` is now
absolute, resolved by `policy.load_sources` from `AppConfig.root`) and
added a regression test. But the *class* of bug is unaddressed:

* `AppConfig` resolves paths relative to the **config file's own
  directory**, which is correct — but nothing enforces that a new path
  must go through it.
* `tests/corpus_helpers.make_config` builds an `AppConfig` rooted at
  `tmp_path`, but nothing *prevents* a test from touching the real root.
* There is no single audited list of roots, and nothing logs them.

**Phase II introduces an explicit, absolute data root** with a startup
log of every resolved root, and a test-side guard that refuses any write
to the production root and any use of the real database.

---

## 5. What Phase II must not disturb

| Invariant | Where it is enforced today |
|---|---|
| No accept without recorded evidence | `rights.evaluate` — the evidence gate runs after every branch |
| Age/death-date never proves anything | `rights.note_age_inference` records the refusal |
| Nothing unknown reaches the index | `INDEXABLE_STATES == {INGESTED, INDEXED}`, checked in three independent places |
| Downloaded text is data, never instruction | `untrusted_content` on every chunk; fenced classifier prompt |
| Official interfaces only, no crawling | Asserted by test: no adapter name may contain scrape/crawl |
| robots.txt, Retry-After, rate limits respected | `BoundedFetcher`, the single network path |
| The export guard | `reporting.export_safe`, refusing with exit code 2 |
| 635 pre-existing Field Horizon tests | Must continue to pass unchanged |

Phase II adds capability and rights **structure**. It must not relax a
single one of these, and the acceptance criterion "zero rights
violations" is not satisfiable by loosening a rule.

---

## 6. Plan, and how each step relates to a known defect

| Step | Work | Addresses |
|---|---|---|
| 1 | `SourceCapabilities` + `AcquisitionPlan` + 8 new states | The central role conflation (§2) |
| 2 | Rights lattice over 9 components | Defects #1, #2 structurally (§3) |
| 3 | Absolute data roots, test isolation | Defect #8 as a class (§4) |
| 4 | Gallica: SRU → ARK → rights → text/ALTO | Gallica disabled; adds `rendered_text` |
| 5 | Standard Ebooks GitHub | Defect #3 (401) without bypassing auth |
| 6 | Wikisource dumps | robots.txt limitation, via the sanctioned transport |
| 7 | DOAB → OAPEN full text | OAPEN 403; separates discovery from hosting |
| 8 | Europeana broker, IA trusted collections | Both disabled; both are brokers, not hosts |
| 9 | Optional OCR | `OCR_PENDING` instead of failure |
| 10 | Archive canary | Archive mode never run |
| 11 | Timer hardening | Stays disabled |
| 12 | Bounded live pilot + acceptance | — |

---

## 7. Constraints accepted for this phase

* **Live pilot ceiling**: under 100 new documents, under 5 GB, one bulk
  operation at a time.
* **No archive mode.** A canary configuration only.
* **No mass OCR.** Three to five documents.
* **The timer stays disabled.** Only its unit is hardened, and only the
  activation command is documented.
* **No credential bypass anywhere.** Standard Ebooks' 401, OAPEN's 403,
  and Wikimedia's robots.txt are all respected. Where a credential is
  absent the adapter reports `READY_WITH_CREDENTIALS` and the harvester
  continues.
* **Rights rules are never relaxed to raise the document count.** If a
  step yields fewer documents because the lattice is stricter than
  first-match-wins, that is the correct outcome and will be reported as
  such.

---

## 8. Migration approach

Unchanged from Phase I, because it is the only mechanism this repository
has: append `CREATE TABLE IF NOT EXISTS` to the schema string, add
guarded `ALTER TABLE` migrations for columns on existing tables, call
them from `init_db()`, bump `CURRENT_SCHEMA_VERSION` 5 → 6. Purely
additive; no existing table is rewritten and no harvested document is
re-adjudicated by a migration.

New tables expected: `corpus_source_capabilities`,
`corpus_acquisition_plans`, `corpus_rights_components`,
`corpus_dump_state`.

---

## 9. Definition of done for Phase II

- All 953 existing tests still pass.
- New offline tests for every step, and the whole corpus suite re-run
  with **every outbound socket blocked**.
- The mandatory lattice case (PD-US work + CC0 edition ⇒ LOCAL_US_ONLY)
  is a regression test, together with the eight other required cases.
- A test reproduces defect #8 and fails if the working directory is ever
  used to resolve a data path.
- A bounded live pilot including as many of Gallica, Standard Ebooks
  GitHub, Wikisource dump-seed, and OAPEN as respond.
- Zero rights violations, zero incorrect CC0 widening, demonstrated
  idempotence, provenance, cross-source deduplication, at least one
  resumed download, the export guard, and no test write into real data.
- The acceptance report distinguishes: implemented / tested offline /
  tested online / blocked by credentials / blocked by provider /
  deliberately disabled.
