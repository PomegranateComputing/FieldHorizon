# Exports Model (Phase UI-6 item 6)

FABLE §18: JSON/Markdown/CSV/HTML/SVG/PNG depending on content, a
reproducible cycle bundle, and an attribution block on every export.

## What existed before this item

Two ad hoc export buttons, both plain-text/JSON with no shared
implementation: `Console.tsx`'s `exportEventsAsJson` and
`ProvenancePage.tsx`'s `downloadReport` (a hand-built `.txt` blob). No
CSV/HTML/SVG/PNG export existed anywhere.

## What was built

* **Shared export plumbing** (`shell/exportFile.ts`,
  `shell/exportAttribution.ts`): `downloadJson`/`downloadMarkdown`/
  `downloadCsv`/`downloadDataUrl`, every text-based one carrying "Generated
  by Field Horizon / Pomegranate Interactive / Copyright © Pomegranate
  Interactive 2026" verbatim. JSON gets it as a sibling `attribution` field
  (never merged into the data itself — data can be an array just as often
  as an object). PNG can't carry a text footer without redrawing the image,
  so it relies on filename + the page it came from instead.
* **The reproducible cycle bundle** (`pages/cycleBundle.ts`, wired into
  `ProvenancePage.tsx` when the entered id resolves to a real cycle via the
  new `GET /cycles/{id}`): assembles configuration, query, identifiers,
  model, evidence, scores, events, result, provenance, canon state, and
  software versions from real, already-real backend data. Two sections
  needed a new backend route that didn't exist (`GET /cycles/{id}` for a
  single-cycle detail lookup — `/canon` only ever filters by verdict, never
  a specific id; `GET /cycles/{id}/evidence` reading the same
  `cycle_sources` table `replay.py`'s exact-reproduction path already
  reads) plus one more (`GET /config`, exposing `engine.config()` which
  existed but had no route). Exportable as JSON or Markdown.
* **CSV**: CorpusPage's loaded sources list, ContradictionsPage's full list
  (also JSON there).
* **PNG**: WEATHER's radar and history charts, via ECharts' own
  `getDataURL()` — no new dependency.

## Two bundle sections are honestly labeled, not fabricated

* **Evaluation**: the original per-component evaluation breakdown for a
  cycle was never persisted anywhere (`replay.py` documents this — only the
  rolled-up `verdict`/`final_score` survive on the `cycles` row). The bundle
  includes a live re-evaluation of the cycle's own stored fragment via the
  existing `/evaluation/preview`, tagged `recomputed_now: true` in the JSON
  and called out explicitly in the Markdown rendering — a real, current
  computation, not a resurrection of a decision that was never recorded.
* **Contradictions**: there is no cycle/axiom linkage anywhere in the
  contradiction-detection code (`godot_export.build_contradictions` always
  computes the full corpus-wide ranking). The bundle includes that live
  snapshot tagged `scoped_to_this_cycle: false` rather than silently
  implying it's specific to the exported cycle.

## Explicitly out of scope

* **SVG/PNG export for the React Flow graphs** (CANON's genealogy tree,
  SEMANTICS' neighbor graph): React Flow renders nodes as HTML, not a single
  canvas/SVG surface, so a clean image export needs an extra library
  (`html-to-image` or similar) and non-trivial layout capture. Deferred
  rather than adding a new dependency inside this item; ECharts' own
  built-in raster export covered the WEATHER case with zero new
  dependencies, which is the one this item actually shipped.
* **HTML export**: no dedicated HTML renderer was built — the Markdown
  output already round-trips cleanly through any Markdown-to-HTML viewer,
  and no content type in this app needed HTML specifically over Markdown.

## Live verification

Against the real project database (read-only throughout — every export
button only issues GET requests): CorpusPage's CSV export produced a real
6-row file with the real source titles and the attribution line; WEATHER's
radar chart PNG export produced a genuine 102KB image; the reproducible
bundle for real cycle #6 ("the machine as idol", CANON, later retired)
correctly assembled its real query, model, 17 real evidence rows, and both
honesty labels, all downloaded and inspected via Playwright.
