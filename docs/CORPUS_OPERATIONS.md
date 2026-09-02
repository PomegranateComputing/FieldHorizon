# Corpus operations

Running the harvester day to day: configuration, budgets, scheduling,
troubleshooting, backup, and controlled deletion.

---

## 1. Before the first run

```bash
export FIELDHORIZON_HARVESTER_CONTACT="you@example.org"
field-horizon corpus health
```

`corpus health` exits 2 and prints a **suggested budget computed from
your actual free disk** if the storage budget is unset. Copy the numbers
into `config/corpus_policy.yaml` deliberately — a suggestion is not a
default, and the harvester will not adopt one silently.

Put the export in your shell profile. Without it every request carries
`unconfigured-contact`, which institutional policies do not accept.

---

## 2. Budgets

`sync`, `resume`, and `daemon-once` **refuse to start** without both
`max_total_corpus_bytes` and `min_free_disk_bytes`. That refusal is
deliberate: a harvester with no storage ceiling eventually fills the
disk, and doing that to someone's workstation is not an acceptable
failure mode.

```yaml
budget:
  max_items_per_run: 50
  max_download_bytes_per_run: 536870912     # 512 MiB
  max_total_corpus_bytes: 4294967296        # 4 GiB ceiling
  min_free_disk_bytes: 8589934592           # 8 GiB always kept free
  requests_per_minute: 12                   # per-source values override
  max_retries: 3
  request_timeout: 30
  decompressed_size_limit: 536870912
  max_candidates_per_source: 500
  max_file_bytes: 67108864                  # 64 MiB per file
```

Every limit is checked **before** the work that would consume it, and
free disk is re-read on every check — another process can fill the disk
mid-harvest, and a stale reading is as bad as none.

Reaching a limit is a **clean stop**, not a crash: the run finishes,
writes its report, and leaves every document resumable. `corpus resume`
continues from there.

`archive` additionally refuses to run without an explicit budget, and is
never entered implicitly — it must be named on the command line.

---

## 3. Everyday use

```bash
# Look before leaping
field-horizon corpus plan --profile broad --limit 30 --dry-run

# Harvest
field-horizon corpus sync --profile broad --rights-profile local_research_us --max-items 50

# Continue an interrupted run
field-horizon corpus resume

# Deterministic classification only (no model)
field-horizon corpus sync --no-llm

# Where things stand
field-horizon corpus status --json | jq .
field-horizon corpus report --show
field-horizon corpus show <document_id>
```

Reports land in `outputs/corpus/runs/<run_id>.{json,md}`, structured
logs in `logs/corpus/<run_id>.jsonl`, and plans in
`data/corpus/reports/<run_id>_plan.json`.

Reports contain counts, hashes, licences, and URLs — **never document
text**. A log that embedded content would be enormous and would put
untrusted text somewhere that later gets grepped and pasted around.

---

## 4. Scheduling

```bash
FIELDHORIZON_HARVESTER_CONTACT=you@example.org ./tools/install_corpus_timer.sh
# ...and when you are ready:
systemctl --user enable --now fieldhorizon-corpus.timer
```

Or install and enable in one step with `--enable`.

**User units only.** Nothing here touches system systemd, uses sudo, or
edits cron. The installer refuses to install a timer for a harvester
whose `corpus health` does not pass — a unit that fails on every firing
is worse than no unit.

The service runs `corpus daemon-once`: exactly one bounded cycle, then
exit. Despite the name it is not a daemon — no loop, nothing to
supervise. If a run is not yet due (`sync_interval_hours`, default 24)
or another run holds the lock, it exits 0 without working.

The unit treats exit code 2 as success, because 2 means *refused* — a
decision, not a fault. It is sandboxed (`ProtectSystem=strict`,
`ReadWritePaths` limited to `data/`, `outputs/`, `logs/`,
`MemoryDenyWriteExecute`, a syscall filter): the harvester never
executes downloaded content, and the sandbox means even a defect that
tried to could not reach anything outside the corpus.

```bash
systemctl --user list-timers fieldhorizon-corpus.timer
systemctl --user start fieldhorizon-corpus.service     # run once, now
journalctl --user -u fieldhorizon-corpus.service -n 50
loginctl enable-linger $USER                            # run while logged out
./tools/uninstall_corpus_timer.sh                       # removes units only
```

Uninstalling removes the two unit files and nothing else — not the
corpus, database, reports, or logs.

---

## 5. Troubleshooting

**`Refusing to start an autonomous harvest without a storage budget`**
Expected. Copy the suggested values from the message into
`config/corpus_policy.yaml`.

**`Another harvester run holds …/harvest.lock`**
A run is in progress. Locks whose PID is gone are reclaimed
automatically, so a power failure needs no manual cleanup. If a process
really is stuck, remove the lockfile.

**`robots.txt disallows …`**
Working as intended. Wikimedia disallows `/w/`, which includes the
Action API — see [CORPUS_SOURCE_POLICY.md](CORPUS_SOURCE_POLICY.md).
Setting `respect_robots: false` is an operator decision, not a default.

**A source reports `failed` while others succeed**
By design. Failures are contained per source and per document; the run
continues and reports both. Check `corpus report --show`.

**Everything from a source is quarantined**
Read the reason codes: `corpus quarantine`. The usual causes are a
missing per-item rights statement (`NO_LICENSE_STATEMENT`), an
aggregator's metadata licence mistaken for a content licence
(`METADATA_LICENSE_ONLY`), or an untrusted uploader
(`UNTRUSTED_UPLOADER_ASSERTION`). This is the engine working.

**Downloads fail with 403**
The provider is declining automated access. `library.oapen.org` does
this. 403 is treated as final rather than retried, and is not worked
around.

**`no fixture for …` in tests**
A test tried to reach the network. Add the URL to the fixture mapping —
the unit suite must stay offline.

**Ollama unreachable**
Fine. Classification falls back to the deterministic path silently. Use
`--no-llm` to make that explicit.

---

## 6. Rebuilding the index

The database is the source of truth; the corpus tree is derived.

```bash
field-horizon corpus rematerialize   # rewrite files from blobs
field-horizon corpus ingest          # re-ingest into sources/chunks
field-horizon corpus reclassify      # re-run classification
field-horizon corpus rematerialize   # move anything that changed destination
```

`reclassify --low-confidence-only` re-examines just the documents
flagged uncertain — useful after a classifier change or when a local
model becomes available.

All of these are idempotent. Re-ingesting an unchanged document is
detected by comparing the stored normalized hash and costs nothing.

---

## 7. Backup

Back up, in order of importance:

1. **`data/field_horizon.sqlite3`** — the source of truth. Everything
   else is reconstructible from it plus the source catalogues.
2. **`data/corpus/blobs/` and `normalized/`** — saves re-downloading,
   and preserves documents whose upstream copy later disappears.
3. **`data/corpus/metadata/` and `licenses/`** — the provenance
   sidecars and licence evidence.
4. `outputs/corpus/` — reports and attributions.

`data/books/_auto/` and `data/manifestos/_auto/` need no backup: they
are hardlinks or copies of blobs and are rebuilt by `rematerialize`.

Everything under `data/corpus/` is gitignored. Never commit it.

---

## 8. Controlled deletion

**One document:**

```python
from fieldhorizon.config import load_config
from fieldhorizon.corpus.ingestion import withdraw_document
from fieldhorizon.corpus.materialization import dematerialize
from fieldhorizon.corpus.repository import CorpusRepository
from fieldhorizon.corpus.models import State

cfg = load_config("config.yaml")
repo = CorpusRepository(cfg.database)
item = repo.get_item("<document_id>")

withdraw_document(cfg, item.document_id)   # out of the index
dematerialize(cfg, item)                   # remove the _auto file
repo.transition(item.document_id, State.WITHDRAWN, "operator request")
```

This removes the document from retrieval and from the corpus tree while
keeping its blob, sidecar, and full audit trail. `dematerialize` only
deletes files inside a reserved `_auto/` directory and confirms that
before unlinking.

**The whole harvested corpus:**

```bash
rm -rf data/books/_auto data/manifestos/_auto
```

Safe by construction: `_auto/` contains only harvested documents, so
nothing a human placed is touched. Then clear the harvested rows:

```sql
DELETE FROM chunks_fts WHERE canonical_ref IN (
  SELECT c.canonical_ref FROM chunks c JOIN sources s ON s.id = c.source_id
  WHERE s.manifest_id LIKE 'corpus:%');
DELETE FROM chunks WHERE source_id IN (SELECT id FROM sources WHERE manifest_id LIKE 'corpus:%');
DELETE FROM sources WHERE manifest_id LIKE 'corpus:%';
```

The `chunks_fts` delete must come first: it has no foreign key to
`chunks`, so removing the chunks first strands its rows as permanent
ghost search results.

To reset the harvester's own state as well, delete from the `corpus_*`
tables. That discards the audit trail, so prefer withdrawal unless you
genuinely want to start over.

---

## 9. Working without an LLM

Fully supported and the default in tests.

- Classification uses form, lexical, and structural signals. `--no-llm`
  forces it.
- Rights are **always** deterministic — no LLM is ever consulted for a
  rights decision.
- Quality, language detection, normalization, and deduplication are
  deterministic by construction.

With a local model reachable (Ollama, per `config.yaml`), the classifier
adds it as one weighted voice capped at 0.9 — it cannot overturn
confident deterministic agreement. Set `classification.model` in
`config/corpus_policy.yaml` to pin a specific model, or leave it empty
to use `ollama.default_model`.

---

## 10. Reproducibility

Selection is deterministic given the same seed, corpus state, and
candidate pool. `selection_seed` in `config/corpus_policy.yaml`
(default 20260731) drives the exploration jitter, which is derived per
candidate from a hash rather than from a stream — so a candidate's
jitter does not depend on how many were scored before it, and the
ranking stays stable when the pool changes.

Change the seed to re-roll exploration; keep it to reproduce a run.
