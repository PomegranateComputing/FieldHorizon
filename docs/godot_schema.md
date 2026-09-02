# Godot Export Schema

Field Horizon exports five versioned JSON files for local narrative-engine
consumers (Godot, or anything else that can read JSON off disk or over the
local platform API). They are produced by:

- `field-horizon export godot --out exports/godot/` (writes the files), or
- `GET /export/factions`, `/export/doctrines`, `/export/contradictions`,
  `/export/weather`, `/export/beliefs` on the local platform API
  (`field-horizon serve`) -- same payloads, same shape, not written to disk.

Every payload carries a top-level `schema_version` integer. A breaking
change to any field below bumps `schema_version` in
`fieldhorizon/contracts.py` (`SCHEMA_VERSION`) -- consumers should check it
before assuming a field's meaning.

This is a **read-only, descriptive** export. Nothing here is fed back into
retrieval, evaluation, or generation -- it is the engine describing its
own current state for something else to dramatize.

## factions.json

Schools of thought (see `field-horizon schools`), reframed as factions.

```json
{
  "schema_version": 1,
  "factions": [
    {
      "name": "string -- LLM-assigned school name",
      "doctrine_summary": "string -- two-sentence summary of the school's shared doctrine",
      "size": 0,
      "weather_profile": { "hierarchy": 0.12, "entropy": -0.03, "...": 0.0 }
    }
  ]
}
```

- `size`: number of active CANON cycles clustered into this school.
- `weather_profile`: mean reading, per doctrinal-weather axis (see
  `weather.json` below), across just this school's member fragments --
  not the whole corpus. Empty object if none of the school's members have
  a current-version embedding yet.
- Empty `factions` list means no school-clustering run has produced
  results yet (run `field-horizon schools --k auto`).

## doctrines.json

Active (non-retired) CANON fragments.

```json
{
  "schema_version": 1,
  "doctrines": [
    {
      "fragment": "string -- the full canonized text",
      "score": 0.0,
      "lineage_root_ids": [1, 2]
    }
  ]
}
```

- `score`: the fragment's `final_score` at time of promotion (or last
  council re-evaluation).
- `lineage_root_ids`: cycle ids with no recorded parent, found by walking
  `parent_cycle_ids` backward from this doctrine. A doctrine synthesized
  from nothing pre-existing has itself as its only root.
- Capped at the 200 most recent active CANON cycles.

## contradictions.json

Top ontological pressure edges (see `field-horizon ontology`): pairs of
axioms whose categories are declared opposed in `ontology.yaml`, ranked by
pressure score.

```json
{
  "schema_version": 1,
  "contradictions": [
    {
      "statement_a": "string",
      "statement_b": "string",
      "reason": "string -- the opposition's declared reason, e.g. 'unity versus fragmentation'",
      "pressure_score": 0.0
    }
  ]
}
```

- Sorted descending by `pressure_score`; capped at 20 by default.
- Pulled directly from `json_entries` axiom pairs and `ontology.yaml`'s
  `oppositions` list -- not from generated_axioms strata specifically.

## weather.json

The current doctrinal climate (see `field-horizon weather`).

```json
{
  "schema_version": 1,
  "canon": { "hierarchy": 0.12, "...": 0.0 },
  "surface": { "hierarchy": 0.09, "...": 0.0 },
  "surface_history": { "hierarchy": [0.1, 0.11, 0.09], "...": [] }
}
```

- `canon`: live aggregate over every active CANON cycle's fragment
  embedding ("deep weather"). Omits axes with no scored canon yet.
- `surface`: mean of the most recent recorded per-cycle readings,
  regardless of verdict ("surface weather" -- the raw texture of
  everything generated, not just what settled into doctrine).
- `surface_history`: up to the last 30 surface readings per axis,
  oldest-first -- what the dashboard's sparkline renders.
- Axis names come from `ontology.yaml`'s `weather:` block and may be
  edited there; this file does not hardcode which axes exist.

## beliefs.json

Per-school sample fragments, suitable as literal NPC belief seeds (a
faction member's dialogue or interior monologue can quote one directly).

```json
{
  "schema_version": 1,
  "beliefs": [
    {
      "school_name": "string",
      "sample_fragments": ["string", "string", "string"]
    }
  ]
}
```

- Up to 3 sample fragments per school, drawn from that school's own
  cluster members.
- A school with zero available member fragments is omitted from the list
  entirely (not included with an empty `sample_fragments`).
