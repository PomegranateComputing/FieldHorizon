# Field Horizon

A local, closed, recursive canonical-synthesis engine. It ingests sacred
texts, political manifestos, and structured doctrinal axioms; sets four
adversarial personas against each other over the retrieved material;
synthesizes the resulting tension into a single severe theological
fragment; judges that fragment against hard structural and doctrinal
gates; and, when the fragment survives judgment, folds it back into its
own canon as material for the next cycle.

It runs entirely against a local Ollama instance. Nothing it produces is
wired to distribution. It is a closed instrument for studying how
polemic hardens into doctrine — not a publishing pipeline, and it should
not become one without deliberately rearchitecting the boundary that
currently keeps it closed.

## What a cycle is

One cycle is one call to `field-horizon cycle` or `field-horizon
multi-cycle`: a query goes in, and a scored fragment comes out, verdicted
into one of four bins — **CANON**, **USEFUL_FRAGMENT**, **HERESY**, or
**NOISE** — and (if `multi-cycle`) synthesized from a genuine argument
between four agents who are not permitted to agree with each other.

## Architecture

The corpus itself can now be acquired automatically. The **Autonomous
Open Corpus Harvester** (`field-horizon corpus …`) discovers legally
reusable texts in official library catalogues, verifies their rights
against recorded evidence, downloads what passes, normalizes and
deduplicates it, decides `books` or `manifesto`, and feeds the result
into the ingestion path below. It never crawls a site, never bypasses
authentication, and never indexes a document whose rights are unknown.
See [docs/AUTONOMOUS_OPEN_CORPUS_HARVESTER.md](docs/AUTONOMOUS_OPEN_CORPUS_HARVESTER.md).

```
corpus harvester     official catalogues (OPDS, OAI-PMH, SRU) →
(optional, opt-in)   evidence-based rights verification → bounded
                     download → normalization → deduplication →
                     books/manifesto classification → the corpus below
  │
  ▼
query
  │
  ▼
interpreter          semantic expansion: literal terms → concepts,
                     ontology targets, an expanded retrieval query
  │                  (temperature 0 — this call must return JSON)
  ▼
retrieval            FTS5 search over ingested books/manifestos,
                     ranked search over structured JSON axioms,
                     domain-biased routing from ontology.yaml,
                     MMR-selected canon fragments (relevant to the
                     query, mutually dissimilar to each other)
  │
  ▼
adversarial agents   THEOLOGIAN vs MACHINE, MYTHIC vs POLITICAL —
(multi-cycle only)   four memoranda, each instructed to attack its
                     assigned enemy and leave the tension unresolved
  │
  ▼
synthesis            one prose fragment resolving (not averaging)
                     the agents' tension, at the configured high
                     creative temperature
  │
  ▼
evaluation           symbolic density, doctrinal enforcement (cosine
                     similarity against axiom embeddings, keyword
                     fallback), length/structure scores, a statistical
                     penalty for over-used motifs, hard gates → verdict
  │
  ▼
critic / rewrite     if the verdict is weak, a critic pass diagnoses
(conditional)        the fragment and a rewrite pass attempts to save it;
                     the better-scoring version wins
  │
  ▼
canon feedback       CANON fragments are embedded and their 3-grams
                     recorded, so future cycles retrieve them (via MMR)
                     and future evaluations penalize their overused
                     phrases (via the motif tracker) — the loop closes
```

Two more subsystems sit beside the per-cycle loop:

- **Ontological pressure graph** (`fieldhorizon/ontology.py`): every pair
  of JSON axioms whose categories are declared opposed in `ontology.yaml`
  gets a pressure edge. `field-horizon generate-axioms` sends each
  high-pressure edge's two actual axioms to the LLM, asks for one new
  axiom neither side could accept but both imply, routes the result
  through a critic for a PROMOTE/REJECT verdict, and writes survivors to
  a new `generated_axioms_NNNN.json` stratum — canon that mutates itself
  under critical supervision, one versioned layer at a time.
- **Rust pressure core** (`fieldhorizon-rust/`): an independent
  reimplementation of the same pressure graph over the full merged JSON
  corpus, reading the same `ontology.yaml`, so the two engines can never
  disagree about what opposes what. Falls back gracefully — loudly, not
  silently — when the binary isn't built.

## Install

Requires Python 3.11+, [Ollama](https://ollama.com) running locally, and
(optionally) a Rust toolchain for the pressure-core parity engine.

```bash
git clone <this repo>
cd FieldHorizon

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

ollama pull hermes3:8b          # or your configured default_model
ollama pull nomic-embed-text    # embeddings: MMR canon selection,
                                 # doctrinal enforcement, axiom mutation

field-horizon init
field-horizon ingest --all
```

Optional, for the Rust pressure core:

```bash
cd fieldhorizon-rust && cargo build --release && cd ..
```

## Quick start

```bash
# A single-pass cycle, no critic/rewrite.
field-horizon cycle --query "the machine as idol"

# The real pipeline: four adversarial agents, synthesis, critic-gated
# rewrite, canon feedback.
field-horizon multi-cycle --query "bureaucracy versus the logos"

# See what accumulated.
field-horizon status
field-horizon index
```

Every cycle writes a full markdown log to `logs/` and a copy to
`outputs/{canon,useful,heresy,noise}/` keyed by verdict; `field-horizon
index` rebuilds `outputs/CYCLE_INDEX.md` from the database (which is the
actual source of truth — the markdown files are generated views, not the
record of what happened).

## Command reference

| Command | What it does |
|---|---|
| `init` | Create the SQLite database and data directories. |
| `ingest --all` | Ingest books, JSON corpora, and manifestos (or `--books`/`--json`/`--manifestos` individually). |
| `status` | Row counts: sources, chunks, JSON entries, cycles. |
| `cycle --query Q` | Single-pass cycle. `--auto-rewrite` to enable the critic/rewrite pass. |
| `multi-cycle --query Q` | Full adversarial pipeline. `--no-auto-rewrite` to disable the critic/rewrite pass (on by default). |
| `export-codex` | Dump the last 50 cycles' responses to `outputs/FIELD_HORIZON_CODEX.md`. |
| `index` | Rebuild `outputs/CYCLE_INDEX.md` from the database. |
| `ontology --limit N` | Export the full pressure graph (`outputs/ontology/`). |
| `axiom-candidates --min-score S` | Preview LLM-mutated axiom candidates for pressure edges above `S`, no critic gate. |
| `generate-axioms --min-score S [--ingest]` | Same mutation step, critic-gated; writes a new `generated_axioms_NNNN.json` stratum. `--ingest` loads it immediately. |
| `rust-pressure --input PATH` | Run the Rust pressure core over one merged JSON file. |
| `rust-pressure-all` | Run it over the full corpus (cached on corpus fingerprint). |
| `lineage AXIOM_ID` | Walk a generated axiom's provenance back to its source axioms and raw material. See below. |
| `backfill-embeddings` | Embed any CANON fragment that predates the embedding model, or whose write-time embed call failed. |
| `corpus …` | The Autonomous Open Corpus Harvester — see below. |

### Corpus harvester

| Command | What it does |
|---|---|
| `corpus health` | Pre-flight: contact address, config, storage budget, disk. Exits 2 with a suggested budget if unconfigured. |
| `corpus sources` / `corpus profiles` | Configured sources and their trust; the seed/broad/archive acquisition profiles. |
| `corpus plan --dry-run` | Discover and rank without downloading anything. |
| `corpus sync` | A full bounded cycle: discover, plan, acquire, classify, ingest. |
| `corpus resume` | Continue whatever an interrupted run left behind. |
| `corpus status [--json]` / `corpus report --show` | Corpus state and per-run reports. |
| `corpus audit-rights --stale-after-days N` | Re-verify licence evidence; de-index anything stale. |
| `corpus export-safe --rights-profile P --output DIR` | Package only what a rights profile permits; refuses otherwise. |
| `corpus show DOC_ID` | Full dossier: rights decisions, evidence, classification, state history. |

Requires `FIELDHORIZON_HARVESTER_CONTACT` and a configured storage
budget; both are refusals rather than warnings. Adds no dependencies and
works with no LLM (`--no-llm`). Full documentation in
[docs/AUTONOMOUS_OPEN_CORPUS_HARVESTER.md](docs/AUTONOMOUS_OPEN_CORPUS_HARVESTER.md),
[CORPUS_SOURCE_POLICY.md](docs/CORPUS_SOURCE_POLICY.md),
[CORPUS_RIGHTS_AND_EXPORT.md](docs/CORPUS_RIGHTS_AND_EXPORT.md), and
[CORPUS_OPERATIONS.md](docs/CORPUS_OPERATIONS.md).

All generation subcommands accept `--model`, and most accept narrower
overrides (`--critic-model`, `--rewrite-model`, `--interpreter-model`,
`--agent-model`, `--synthesizer-model`) so different stages can run
different local models.

## Configuration

`config.yaml` at the repo root. Paths are resolved relative to the
config file's own location, not the working directory, so the CLI works
from anywhere.

```yaml
ollama:
  base_url: "http://localhost:11434"
  default_model: "hermes3:8b"
  temperature: 1.25        # agents + synthesizer: high, for prose
  top_p: 0.95
  repeat_penalty: 1.08
  num_ctx: 8192

embeddings:
  model: "nomic-embed-text"  # any call needing structured JSON back
                              # (interpreter, axiom mutation, axiom
                              # critic) overrides this to temperature 0
                              # per-call, not here
```

The generation temperature is deliberately split: the agents and the
synthesizer keep the configured high creative temperature (they're
writing doctrine), while the interpreter, axiom mutator, and axiom critic
run at temperature 0 (they're returning JSON that has to parse).

## Extending the ontology

The ontology — domains, retrieval routing, evaluator keywords, rewrite
directives, and the opposition pairs that drive the pressure graph — is
declarative, in `ontology.yaml`, loaded by both Python and Rust. Adding a
tradition requires no code changes:

1. **Add a domain block** to `ontology.yaml`:

   ```yaml
   domains:
     kabbalah:
       hints: [kabbalah, sefirot, ein_sof, tzimtzum, emanation]
       expansions: [emanation, concealment, vessel, light, contraction]
       sources: [sacred_bible, red_flags]
       keywords: [sefirot, emanation, concealment, vessel]
       rewrite_directive: >-
         explicitly develop emanation, concealment, the withdrawn light,
         and the shattering of the vessels
   ```

2. **Declare its oppositions**, if any:

   ```yaml
   oppositions:
     - [kabbalah, bureaucracy, "emanation versus administration"]
   ```

3. **Add a corpus file** — `data/json_corpus/kabbalah.json`, a JSON list
   of axiom entries (`id`, `category`, `tradition`, `statement`, `gloss`,
   `severity`, `mutation_potential`, ...). See any existing corpus file
   for the shape.

4. `field-horizon ingest --json`, then query it: `field-horizon
   multi-cycle --query "tzimtzum and the withdrawn light"` routes
   retrieval toward the new domain's declared sources automatically.

Nothing in `retrieval.py`, `evaluate.py`, `ontology.py`, or `rewrite.py`
needs to change. The Python and Rust pressure engines both read this same
file, so they cannot disagree about what opposes what.

## Lineage

`field-horizon generate-axioms` writes axioms whose provenance is
recorded on the axiom itself: which pressure edge produced it, which two
source axioms fed that edge, and which stratum it belongs to.
`field-horizon lineage <axiom_id>` walks that chain and renders it as a
tree — through the pressure edge, to the source axioms, and (where a
source axiom's gloss cites a raw distilled sentence) down to that raw
material:

```
$ field-horizon lineage generated_axiom_0001_0004
generated_axiom_0001_0004 (recursive_axiom): The undecidable truth, in its
very elusiveness, becomes a sacred provocation...
└── stratum 0001
    └── pressure edge undecidable_truth vs bureaucracy (score=1.36) -- truth
        beyond procedure
        ├── source `godel_0004` (undecidable_truth): An undecidable
        │   proposition is not meaningless; it is a truth displaced beyond
        │   the court that tries to judge it.
        └── target `bureaucracy_0003` (bureaucracy): A form is a prayer
            addressed to a dead institution.
```

It recurses through multiple strata — an axiom generated from another
generated axiom still resolves all the way down.

## Development

```bash
pytest                                  # unit tests, no Ollama required
ruff check fieldhorizon/ tests/ scripts/
mypy fieldhorizon/
```

`scripts/redistill_metadata.py` re-estimates per-item severity,
mutation_potential, and targets for a distilled corpus file via a
temperature-0 LLM call, writing to a new `_v2.json` file rather than the
source. `scripts/make_review_dump.sh` regenerates a full architecture
dump for review, built from git's own tracked-and-not-ignored file list
(so `.gitignore` — not a hand-maintained exclude list — decides what a
reviewer sees). Neither script runs as part of the normal pipeline;
invoke them explicitly.

`FIELD_HORIZON_REVIEW.md` is the architecture review this codebase has
been implementing against; it records what was found and, section by
section, what fixed it.
