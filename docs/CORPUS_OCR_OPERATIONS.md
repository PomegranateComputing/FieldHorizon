# Corpus OCR Operations

OCR is optional, off by default, and bounded. This document explains what
happens to a scanned document, how to enable OCR if you want it, and why
you probably do not need it.

## A scan is a state, not a failure

Phase I refused scanned PDFs and called it a normalization failure. It
was right to refuse and wrong to call it a failure.

A page image is not a broken document. It is a document whose text has
not been extracted, and the difference decides what an operator does
about it: a failure is something you fix in the code, a missing
capability is something you install.

So `normalize_pdf` raises `NeedsOCR`, which is deliberately **not** a
subclass of `NormalizationError`. The orchestrator treats a
`NormalizationError` as final — correctly, because a malformed EPUB will
still be malformed next week — and routes `NeedsOCR` to `OCR_PENDING`
instead.

`OCR_PENDING` is in `NON_ERROR_ACQUISITION_STATES`, is not terminal, and
can transition to `NORMALIZED`. Install a backend a month later and those
documents become processable with nothing re-downloaded.
`RunStats.ocr_pending` counts them separately from errors.

## You probably do not need it

Provider-supplied text is better than anything local OCR will produce. It
is the provider's own extraction, usually proofread, and it costs one
request instead of CPU-hours:

| Source | What it gives |
|---|---|
| OAPEN | An already-extracted text layer in a DSpace `TEXT` bundle, `text/plain` |
| Wikisource | Wikitext, transcribed and proofread by humans |
| Gutenberg | Plain text |
| Standard Ebooks | XHTML, typeset from proofread sources |
| Gallica | `.texteBrut` where reachable, and ALTO where offered |

OCR must never become a shortcut around implementing one of these. If a
provider publishes text and this harvester is OCR-ing its PDFs instead,
that is a bug in the adapter, not a reason to enable OCR.

## Three gates

All three must open before a single document is OCR'd.

### 1. A backend must exist

Any one of:

```bash
pip install 'fieldhorizon[ocr]'      # pytesseract or ocrmypdf
sudo apt install tesseract-ocr       # or ocrmypdf, found on PATH
export FIELDHORIZON_OCR_WORKER=https://ocr.internal/queue
```

Detection uses `importlib.util.find_spec` rather than `import`, and never
contacts a configured worker. `corpus health` must stay cheap and
side-effect-free.

### 2. The policy must allow it

```yaml
# config/corpus_policy.yaml
ocr:
  enabled: true
```

Installing tesseract must not silently enable OCR across a corpus.

### 3. The per-run ceiling must not be exhausted

```yaml
ocr:
  max_documents_per_run: 5
```

Deliberately tiny. OCR is the only operation in this harvester costed in
CPU-hours rather than bytes, and a corpus-wide OCR run should be a
decision somebody made rather than something discovered afterwards.
Documents past the ceiling stay in `OCR_PENDING` and are picked up by a
later run.

## Checking the state

```bash
field-horizon corpus health
```

```
│ OCR  │ not installed │ no OCR backend installed: `pip install            │
│      │               │ 'fieldhorizon[ocr]'`, put tesseract or ocrmypdf   │
│      │               │ on PATH, or set $FIELDHORIZON_OCR_WORKER          │
```

The row distinguishes three things:

- **not installed** — no backend found
- **available, disabled** — a backend exists, `ocr.enabled` is false
- **ready** — all three gates open

That distinction is why a corpus full of `OCR_PENDING` documents has an
obvious explanation rather than requiring two config files to be read.

## Finding what is waiting

```bash
field-horizon corpus status
field-horizon corpus show <document-id>
```

A document in `OCR_PENDING` records the page count and the OCR health
report as of the moment it was parked, so it is clear whether it stopped
because no backend existed, because the policy said no, or because the
ceiling was reached.

## Rights do not change

OCR produces text; it produces no rights. The `OCR` component is in
`INSUFFICIENT_ALONE` — an open licence on the OCR text of a scan
establishes nothing about the underlying work. A document that would
quarantine without OCR quarantines with it.

## Do not run bulk OCR

Stated plainly because it is the obvious next step and it is the wrong
one. The ceiling defaults to 5 so that an accidental corpus-wide run is
impossible rather than merely unlikely. If you genuinely need OCR at
scale, run it as an external worker on a machine that is not also serving
retrieval, and point `$FIELDHORIZON_OCR_WORKER` at it.

## Related

- [CORPUS_SOURCE_GRAPH.md](CORPUS_SOURCE_GRAPH.md) — `RENDERED_TEXT` and why it matters
- [CORPUS_RIGHTS_LATTICE.md](CORPUS_RIGHTS_LATTICE.md) — why OCR is insufficient alone
