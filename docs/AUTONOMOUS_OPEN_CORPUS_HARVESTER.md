# Autonomous Open Corpus Harvester

Field Horizon acquires its own corpus: it discovers legally reusable
texts in official library catalogues, verifies their rights against
recorded evidence, downloads what passes, normalizes and deduplicates
it, decides whether each document belongs in `books` or `manifesto`, and
feeds the result into the existing ingestion and retrieval pipeline.

It runs bounded, resumable, and offline-testable, with no paid API and
no required LLM.

---

## 1. What it will and will not do

**Will:** read official catalogues (OPDS, OAI-PMH, SRU, documented
APIs, published catalogue files); verify licences against evidence it
records; download at a paced, identified, robots-respecting rate;
normalize, deduplicate, classify, and index.

**Will not:** crawl a website; scrape HTML where an official interface
exists; bypass authentication, a paywall, a CAPTCHA, or a rate limit;
accept a licence on inference; index anything whose rights are unknown;
execute or follow anything found inside a downloaded document.

Five rules govern everything below, and each is enforced in code rather
than by convention:

| Rule | Where it lives |
|---|---|
| No accept without recorded evidence | `rights.evaluate` refuses an accept when `signal.evidence` is empty |
| Age and author death dates are never proof | `rights.note_age_inference` records that they were *seen and refused* |
| Metadata licence ≠ content licence | `RightsSignal` keeps them apart; `METADATA_LICENSE_ONLY` quarantines the confusion |
| Unknown, ambiguous, or contradictory ⇒ quarantine, never index | `INDEXABLE_STATES` is `{INGESTED, INDEXED}` and nothing else |
| Downloaded text is data, never instruction | Every chunk carries `untrusted_content: true`; the classifier's prompt fences it |

---

## 2. Installation

Nothing to install. The harvester adds **zero dependencies** — it uses
`requests`, `PyYAML`, and the standard library, all already present.

```bash
export FIELDHORIZON_HARVESTER_CONTACT="you@example.org"
field-horizon corpus health
```

The contact address is mandatory and lives only in the environment.
Institutional harvesting policies require a reachable operator, and it
goes into the `User-Agent` of every request. It is never written to a
config file.

`corpus health` checks the contact, the two config files, the storage
budget, and disk space, and exits 2 if anything is missing — printing a
concrete suggested budget computed from your actual free space.

---

## 3. The pipeline

```
DISCOVERY → RIGHTS → ELIGIBILITY → SELECTION → DOWNLOAD → RAW STORAGE
    → NORMALIZATION → QUALITY → DEDUPLICATION → CLASSIFICATION
    → MATERIALIZATION → INGESTION → INDEXING → AUDIT
```

Every stage is entered from the database alone, which is what makes the
whole thing resumable: `corpus resume` does not replay a journal, it
looks at what state each document is in and runs the stage that state
implies.

### States

`DISCOVERED → RIGHTS_PENDING → {RIGHTS_ACCEPTED | RIGHTS_REJECTED |
RIGHTS_QUARANTINED} → QUEUED → DOWNLOADING → DOWNLOADED → NORMALIZED →
{CLASSIFIED_BOOKS | CLASSIFIED_MANIFESTO} → MATERIALIZED → INGESTED →
INDEXED`

plus `QUALITY_REJECTED`, `DUPLICATE`, `STALE`, `WITHDRAWN`,
`FAILED_RETRYABLE`, `FAILED_FINAL`.

Transitions are validated against an explicit table
(`models._TRANSITIONS`); an illegal one raises rather than being
written. Every transition writes a `corpus_state_events` row with its
reason, run id, and pipeline version, in the same transaction as the
state change — so a document can never be observed as `NORMALIZED`
without its hash or `MATERIALIZED` without its path.

---

## 4. Quick start

```bash
# 1. See what is configured
field-horizon corpus sources
field-horizon corpus profiles

# 2. Discover and rank without downloading anything
field-horizon corpus plan --profile broad --rights-profile local_research_us --limit 30 --dry-run

# 3. A real, bounded harvest
field-horizon corpus sync --profile broad --rights-profile local_research_us --max-items 50

# 4. What happened
field-horizon corpus status
field-horizon corpus report --show
```

`sync` always writes its plan to disk *before* the first acquisition, so
what a run intended is recoverable even if it dies mid-download.

---

## 5. Commands

| Command | Purpose |
|---|---|
| `corpus sources` | Configured sources, trust, and host allowlists |
| `corpus profiles` | The seed / broad / archive acquisition profiles |
| `corpus health` | Pre-flight: contact, config, budget, disk |
| `corpus discover` | Walk catalogues; downloads no content |
| `corpus plan` | Rank admissible candidates; writes the plan |
| `corpus sync` | Full bounded cycle |
| `corpus resume` | Continue an interrupted run |
| `corpus status [--json]` | Corpus state, distributions, disk |
| `corpus report [--run-id] [--show]` | Markdown + JSON run report |
| `corpus audit-rights --stale-after-days N` | Re-verify licences; de-index what went stale |
| `corpus quarantine` | Quarantined candidates and why |
| `corpus reclassify [--low-confidence-only]` | Re-run books/manifesto classification |
| `corpus rematerialize` | Move documents whose destination changed |
| `corpus ingest` | Ingest materialized documents |
| `corpus export-safe --rights-profile P --output DIR [--filter]` | Package the permitted subset |
| `corpus attributions` | Regenerate `ATTRIBUTIONS.{jsonl,md}` |
| `corpus show <document_id>` | Full dossier: rights, classification, history |
| `corpus daemon-once` | One bounded cycle, then exit (for the timer) |

**Exit codes:** `0` success, `1` failure, `2` **refusal** — budget not
configured, lock held, export blocked. A refusal is a decision, not a
fault, which is why the systemd unit treats 2 as success.

---

## 6. Acquisition profiles

| Profile | Items/run | Min source trust | Notes |
|---|---:|---:|---|
| `seed` | 20 | 0.85 | Small, high-confidence starter corpus |
| `broad` | 50 | 0.50 | Multilingual with diversity quotas — the daily mode |
| `archive` | 5000 | 0.50 | Bulk dumps. **Never launched implicitly**; requires an explicit storage budget |

---

## 7. Rights profiles

| Profile | Accepts | Scope required |
|---|---|---|
| `local_research_us` | PD, PD-US, PDM, CC0, CC BY, CC BY-SA, US federal works | WORLDWIDE or LOCAL_US_ONLY |
| `release_worldwide` | PD, PDM, CC0, CC BY, CC BY-SA | WORLDWIDE only |
| `strict_public_domain` | PD, PDM, CC0 | WORLDWIDE only |

Always refused, under every profile: unknown, Copyright Not Evaluated,
In Copyright, All Rights Reserved, any NC or ND licence, borrow-only,
access-restricted, contradictory metadata, a bare "free"/"open" claim,
an age-based or death-date-based inference, and a metadata-only licence.

**The scope distinction is the one that matters.** A Project Gutenberg
text is public domain *in the United States*, which Gutenberg states
plainly. That is an accept under `local_research_us`, marked
`LOCAL_US_ONLY`, and a **quarantine** under `release_worldwide`.
`corpus export-safe --rights-profile release_worldwide` refuses to
package it. See [CORPUS_RIGHTS_AND_EXPORT.md](CORPUS_RIGHTS_AND_EXPORT.md).

---

## 8. books or manifesto

The distinction is **functional, not topical**:

> A political novel is `books`. A history of a revolution is `books`.
> A text calling for that revolution is `manifesto`.

Four signals combine: bibliographic form, multilingual lexical evidence
(density-normalized, so a short manifesto is not outvoted by a long
book), document structure, and — optionally — a local LLM over a
*sample*, never a whole book.

The model is one weighted voice capped at 0.9, so it cannot overturn
confident deterministic agreement. If it is unreachable, returns
malformed JSON, or emits a destination that is not one of the two, the
deterministic result stands. **The system works with no LLM at all**;
pass `--no-llm` to require that.

Low confidence produces the more probable destination plus
`classification_low_confidence=true` and a place in the re-evaluation
queue. It never produces a quarantine — quarantine is for rights alone.

---

## 9. Storage

```
data/corpus/
  blobs/aa/bb/<sha256>          immutable raw downloads
  normalized/aa/bb/<sha256>.txt normalized UTF-8 text
  licenses/aa/bb/<sha256>.txt   licence text kept as evidence
  metadata/<document_id>.json   full per-document provenance sidecar
  quarantine/<document_id>.json quarantine dossiers
  locks/ reports/ manifests/

data/books/_auto/               materialized → source_type "book"
data/manifestos/_auto/          materialized → source_type "manifesto"
```

Two details worth knowing:

* **Sidecars live in `data/corpus/metadata/`, never beside the text.**
  `ingest_books` globs `*.txt` recursively, so a sidecar next to a
  document would be ingested *as corpus content*.
* **`_auto/` is reserved.** Everything the harvester writes is inside
  it, so `rm -rf data/books/_auto` removes the entire harvested corpus
  and touches nothing a human placed.

All of `data/corpus/` is gitignored and reconstructible from the
database plus the source catalogues.

---

## 10. Integration with Field Horizon

Harvested documents become ordinary `sources` and `chunks` rows, written
in exactly the shape `_ingest_manifest_entry` already writes them — so
retrieval, hybrid scoring, embeddings, concept tagging, and
fingerprinting work on them with **no changes to those subsystems**.

* `sources.manifest_id` is `corpus:<document_id>`, which makes ingestion
  idempotent via the existing partial unique index and makes every
  harvested row separable from curated ones.
* `sources.source_type` is the singular `"book"` or `"manifesto"` that
  retrieval routing already knows.
* `sources.weight` is capped below 1.0: a hand-curated source defaults
  to 1.0, and an automatically acquired text should not outrank a
  deliberately chosen one.
* Each chunk's `metadata` carries the document id, licence, canonical
  URL, hash, and `untrusted_content: true`.

`retrieval.attach_corpus_provenance()` enriches results with that
provenance and marks them untrusted. Chunks from hand-curated sources
pass through with no `provenance` key at all — historical documents have
no sidecar and keep working exactly as before.

---

## 11. Safety

**Network.** HTTPS only (per-source opt-in for the rare institutional
endpoint without TLS); per-source host allowlists with dot-boundary
matching; redirects followed one hop at a time and re-validated each
time; a redirect to plain HTTP on a trusted host is *upgraded*, never
followed; robots.txt respected; `Retry-After` honoured; per-host rate
limiting; bounded timeouts, retries, and body sizes enforced during
streaming rather than from `Content-Length`.

**Content.** Real MIME sniffed from bytes, not from the server's
header; executables and macro-bearing formats refused outright; XML
parsed with DOCTYPE and ENTITY declarations rejected *before* parsing
(killing XXE and billion-laughs rather than mitigating them); archives
checked for zip-slip and decompression bombs; HTML reduced to text by a
stdlib tokenizer with no scripting concept at all.

**Prompt injection.** A downloaded document may contain instructions
aimed at this system. It is never read as one: the classifier's system
prompt states the sample is untrusted data, the sample is fenced with a
per-call random delimiter the document cannot guess, and the only thing
the model can influence is a books/manifesto label and some floats. It
cannot cause a fetch, a write, a shell command, or a rights decision —
the return type has no way to express any of those. A URL found inside a
document is never followed.

**Filesystem.** Every path built from harvested metadata passes through
`safe_filename` and `ensure_within`. No metadata ever reaches a shell or
a SQL string; SQL column names are validated against allowlists.

**Secrets.** Environment variables only, redacted from every log line.

---

## 12. Further reading

- [CORPUS_SOURCE_POLICY.md](CORPUS_SOURCE_POLICY.md) — what each source
  is, why it is enabled or not, and how to add one
- [CORPUS_RIGHTS_AND_EXPORT.md](CORPUS_RIGHTS_AND_EXPORT.md) — the
  rights engine, evidence, quarantine, audit, and the export guard
- [CORPUS_OPERATIONS.md](CORPUS_OPERATIONS.md) — running it, the timer,
  troubleshooting, backup, and controlled deletion
