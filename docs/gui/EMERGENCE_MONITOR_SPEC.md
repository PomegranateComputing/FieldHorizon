# EMERGENCE MONITOR — specification only (Implementation Brief IV, Phase UI-5 item 10)

FABLE §19 is explicit and conditional:

> Si le dépôt contient réellement des notions comme : Basilisk; emergence;
> anomaly; recursion; attractor; rupture sémantique; détection d'entité —
> créer une section expérimentale nommée EMERGENCE MONITOR [...] Si ces
> fonctions n'existent pas réellement, créer uniquement une spécification
> documentée et ne pas les présenter comme disponibles.

This document *is* that specification. No route, no engine method, no
screen was built for this item — the audit below is the reason why, and
doubles as the real backlog if any of these signals are ever genuinely
implemented.

## Audit method

A repo-wide search (`grep -rin` across `fieldhorizon/*.py`) for each term
FABLE §19 names, plus the three candidate signal families Implementation
Brief IV's own phase description paraphrases them as (motif-frequency
spikes, pressure anomalies, weather discontinuities). Every hit was read
in context, not just counted.

## Per-term findings

| FABLE §19 term | Real referent in this codebase? | Detail |
|---|---|---|
| Basilisk | No | Zero occurrences anywhere in `fieldhorizon/`. |
| recursion | No | One occurrence, in `agents.py`'s persona prompt for one of the four adversaries ("Interpret through systems, automation, infrastructure, logs, databases, computation, recursion, feedback loops..."). This is stylistic instruction text handed to an LLM, not a computed signal about the corpus's own state. |
| attractor | No | Zero occurrences. |
| rupture sémantique (semantic rupture) | No | Zero occurrences, in French or English. |
| détection d'entité (entity detection) | Partial | `chunk_entities` (concepts.py's tagging pipeline) real and already exposed — SEMANTICS' neighborhood explorer (Phase UI-5 item 6) already covers browsing/exploring entities. But this is enumeration, not detection of a *rare or anomalous* entity-related state; nothing flags an entity as newly emergent or statistically unusual. |
| anomaly / motif-frequency spikes | Partial (data only) | `canon.load_motif_counts` + `canon_loop_penalty` (evaluate.py) track *current* 3-gram frequency over the last `MOTIF_WINDOW` canon fragments and penalize overuse *right now*. There is no persisted history of that count over time, no baseline, and no threshold for "this frequency just spiked" — only a static "is this overused at this instant" check. |
| pressure anomalies | Partial (data only) | `ontology.build_pressure_edges` (godot_export.build_contradictions, Phase UI-5 item 7) computes real pressure scores live. `diagnostics.py`'s own capability entry says plainly: "computed live and never persisted; no severity typology." With no persisted history, there is nothing to compare a current score against to call it anomalous — every score is only ever seen once, in isolation. |
| weather discontinuities | Partial (data only, closest candidate) | `weather_readings` (Phase UI-5 item 1) is a genuine, persisted time series — the only one of the five candidates with real history to work from. But `axis_history` (weather.py) returns values in insertion order only, with no timestamp threaded through to the API response (a deliberate scope decision at WEATHER build time, documented in that item's own commit), and no function anywhere computes a delta, a rolling baseline, or a discontinuity threshold. |

## Conclusion

No candidate signal has a real, already-computed detector behind it. Every
one of them would require inventing new detection logic (a baseline, a
window, a threshold, a persistence layer for historical comparison) from
nothing — exactly the situation FABLE §19 says should ship as a
specification, not as a screen presenting invented thresholds as if they
were established science. Per that instruction: no EMERGENCE MONITOR route,
engine method, or UI was built for this item.

## What a real implementation would need, per candidate

Recorded here as a backlog, not authorized for autonomous implementation —
each would need its own scope decision (matching how WEATHER/SCHOOLS/DREAMS
in this same phase each got an explicit sign-off for their own real backend
extensions):

- **Weather discontinuity** (strongest candidate): thread real timestamps
  through `axis_history`/`AxisHistory` (currently insertion-order only);
  define a concrete statistical test (e.g. a z-score against a trailing
  window's mean/stddev per axis); a new `weather.detect_discontinuities()`
  function; persistence of *flagged* discontinuities so the monitor has
  its own honest history, not just a live recomputation every request.
- **Motif-frequency spike**: persist `chunk_motifs` counts as a real time
  series (currently only a live `GROUP BY` snapshot); define "spike"
  against a trailing baseline, not the single fixed `MOTIF_FREQUENCY_RATIO`
  threshold `canon_loop_penalty` already uses for a different purpose
  (loop suppression during evaluation, not monitoring).
- **Pressure anomaly**: start persisting `build_pressure_edges`' output
  (currently recomputed live and explicitly never stored, per
  `diagnostics.py`) so a "this pressure score is unusually high" claim has
  a real historical distribution to be unusual *against*.
- **Rare/unusual entity emergence**: define what "rare" means against
  `chunk_entities`' real frequency distribution (a first-seen entity? one
  crossing a mention-count threshold within a time window?) — currently
  undefined, no code computes this at any granularity.

None of the above is scheduled; this section exists so a future session
doesn't have to re-run this same audit from zero.
