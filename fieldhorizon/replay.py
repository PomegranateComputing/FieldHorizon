from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from .config import AppConfig
from .db import connect
from .embeddings import load_cycle_embedding
from .manifests import RunManifestRepository, build_manifest_fingerprints, find_manifest_for_cycle
from .prompting import build_prompt
from .schools import _kmeans
from .synthesis import generate_fragment
from .temporal import canon_valid_as_of


class ReplayNotFoundError(LookupError):
    """No cycle with the given id, or it has no stored prompt to replay."""


@dataclass(frozen=True)
class ReplayResult:
    cycle_id: int
    model: str
    old_fragment: str
    new_fragment: str
    diff: str


@dataclass(frozen=True)
class ConfigDrift:
    field: str
    recorded: str | int | None
    current: str | int | None
    changed: bool


@dataclass(frozen=True)
class ConfigReplayResult:
    cycle_id: int
    run_id: str
    drifts: list[ConfigDrift]

    @property
    def any_drift(self) -> bool:
        return any(d.changed for d in self.drifts)


@dataclass(frozen=True)
class EvidenceSource:
    source_kind: str
    ref: str
    content: str


@dataclass(frozen=True)
class EvidenceReplayResult:
    cycle_id: int
    verdict: str | None
    final_score: float | None
    sources: list[EvidenceSource]


def replay_cycle(cfg: AppConfig, cycle_id: int, model: str | None = None) -> ReplayResult:
    """
    Re-executes a cycle's exact stored prompt against the same model
    string (cycles.prompt is recorded verbatim at cycle time -- exactly
    what was sent to the model, not just the retrieval query), and diffs
    the resulting fragment against what was originally recorded.

    Sampling nondeterminism means a diff is expected even against an
    unchanged model and prompt -- the point of replay is auditability of
    INPUTS (this prompt, this model, produced this at the time), not
    byte-identical reproduction of outputs.
    """
    with connect(cfg.database) as conn:
        row = conn.execute("SELECT prompt, model, fragment FROM cycles WHERE id = ?", (cycle_id,)).fetchone()

    if row is None or not row["prompt"]:
        raise ReplayNotFoundError(f"No cycle {cycle_id} with a stored prompt to replay")

    replay_model = model or row["model"]
    new_fragment = generate_fragment(cfg, row["prompt"], model=replay_model)
    old_fragment = row["fragment"] or ""

    diff = "\n".join(
        difflib.unified_diff(
            old_fragment.splitlines(),
            new_fragment.splitlines(),
            fromfile=f"cycle_{cycle_id}_original",
            tofile=f"cycle_{cycle_id}_replay",
            lineterm="",
        )
    )

    return ReplayResult(
        cycle_id=cycle_id,
        model=replay_model,
        old_fragment=old_fragment,
        new_fragment=new_fragment,
        diff=diff,
    )


_MANIFEST_DRIFT_FIELDS = (
    "ontology_fingerprint",
    "source_manifest_fingerprint",
    "configuration_fingerprint",
    "dependency_fingerprint",
    "rust_binary_fingerprint",
    "db_schema_version",
    "code_commit",
)


def replay_cycle_level2(cfg: AppConfig, cycle_id: int) -> ConfigReplayResult:
    """
    L2 configuration replay: compares the RunManifest recorded at cycle
    time against the CURRENT environment's fingerprints, field by field.
    These are content-free "did this change" fingerprints (see
    manifests._path_fingerprint), not content diffs -- a `changed=True`
    means the environment has drifted since this cycle ran, not what
    specifically changed.
    """
    manifest = find_manifest_for_cycle(cfg, cycle_id)
    if manifest is None:
        raise ReplayNotFoundError(
            f"No run_manifests row for cycle {cycle_id} -- it predates Phase B, "
            "or manifest recording failed for this cycle (best-effort; see cycle.py)"
        )

    current = build_manifest_fingerprints(cfg)
    drifts = [
        ConfigDrift(field=field, recorded=getattr(manifest, field), current=current[field], changed=getattr(manifest, field) != current[field])
        for field in _MANIFEST_DRIFT_FIELDS
    ]
    return ConfigReplayResult(cycle_id=cycle_id, run_id=manifest.run_id, drifts=drifts)


def replay_cycle_level3(cfg: AppConfig, cycle_id: int) -> EvidenceReplayResult:
    """
    L3 evidence replay: reconstructs the exact selected-evidence set a
    cycle's prompt was built from, via cycle_sources (content stored
    verbatim at cycle time, so this is exact, not best-effort).

    Score decomposition (symbolic_density, doctrinal_enforcement, etc.) is
    NOT reconstructed here -- only the rolled-up verdict/final_score is
    ever persisted (cycles.verdict/final_score); the sub-score breakdown
    computed by evaluate_cycle is never written to the DB, so replaying it
    would mean fabricating a value, not recovering one.
    """
    with connect(cfg.database) as conn:
        cycle_row = conn.execute("SELECT verdict, final_score FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        if cycle_row is None:
            raise ReplayNotFoundError(f"No cycle {cycle_id}")
        source_rows = conn.execute(
            "SELECT source_kind, ref, content FROM cycle_sources WHERE cycle_id = ? ORDER BY rowid", (cycle_id,)
        ).fetchall()

    sources = [EvidenceSource(source_kind=row["source_kind"], ref=row["ref"], content=row["content"]) for row in source_rows]
    return EvidenceReplayResult(
        cycle_id=cycle_id,
        verdict=cycle_row["verdict"],
        final_score=cycle_row["final_score"],
        sources=sources,
    )


@dataclass(frozen=True)
class SchoolsReplayResult:
    run_id: str
    seed: int
    k: int
    identical_partition: bool
    original_partition: list[list[int]]
    replayed_partition: list[list[int]]
    missing_cycle_ids: list[int]


def replay_schools_level4(cfg: AppConfig, run_id: str) -> SchoolsReplayResult:
    """
    L4 deterministic-stage replay: schools clustering is the one
    fully-deterministic multi-step stage in the system -- fixed vectors + k
    + seed always produce the same partition (see schools._kmeans). Dream's
    seed only weights region *selection*, not LLM generation, so only
    schools qualifies for this replay level.

    Re-fetches CURRENT embeddings for the cycle ids that were originally
    clustered and re-runs k-means with the recorded seed/k, then compares
    the resulting partition (as sets of cycle ids per cluster -- school_id
    labels aren't stable across runs, so labels themselves can't be
    compared) against what school_members originally recorded.
    """
    manifest = RunManifestRepository(cfg).get(run_id)
    if manifest is None or manifest.operation != "schools":
        raise ReplayNotFoundError(f"No schools run_manifests row for run_id {run_id!r}")
    if not manifest.random_seeds or "seed" not in manifest.random_seeds or "k" not in manifest.random_seeds:
        raise ReplayNotFoundError(
            f"Run {run_id!r} has no recorded seed/k to replay -- it predates the chosen_k capture"
        )

    seed = int(manifest.random_seeds["seed"])
    k = int(manifest.random_seeds["k"])

    school_ids = [int(oid.split(":", 1)[1]) for oid in (manifest.output_ids or []) if oid.startswith("school:")]
    if not school_ids:
        raise ReplayNotFoundError(f"Run {run_id!r} recorded no school output_ids")

    with connect(cfg.database) as conn:
        run_at_row = conn.execute("SELECT run_at FROM schools WHERE id = ?", (school_ids[0],)).fetchone()
        if run_at_row is None:
            raise ReplayNotFoundError(f"School {school_ids[0]} referenced by run {run_id!r} no longer exists")
        run_at = run_at_row["run_at"]

        member_rows = conn.execute(
            """
            SELECT m.school_id, m.cycle_id FROM school_members m
            JOIN schools s ON s.id = m.school_id
            WHERE s.run_at = ?
            """,
            (run_at,),
        ).fetchall()

    original_groups: dict[int, list[int]] = {}
    for row in member_rows:
        original_groups.setdefault(int(row["school_id"]), []).append(int(row["cycle_id"]))
    original_partition = [sorted(ids) for ids in original_groups.values()]

    original_cycle_ids = sorted(cid for ids in original_partition for cid in ids)
    present_cycle_ids = [cid for cid in original_cycle_ids if load_cycle_embedding(cfg, cid) is not None]
    missing_cycle_ids = [cid for cid in original_cycle_ids if cid not in present_cycle_ids]

    if len(present_cycle_ids) < k:
        # Too many of the originally clustered cycles have lost their embeddings
        # (retired, or predating an embedding backfill) to honestly re-run k clusters.
        raise ReplayNotFoundError(
            f"Only {len(present_cycle_ids)} of {len(original_cycle_ids)} originally clustered cycles still have "
            f"embeddings -- too few to re-run k={k} clustering for run {run_id!r}"
        )

    vectors = np.array([load_cycle_embedding(cfg, cid) for cid in present_cycle_ids])
    labels, _ = _kmeans(vectors, k, seed)

    replayed_groups: dict[int, list[int]] = {}
    for cid, label in zip(present_cycle_ids, labels.tolist(), strict=True):
        replayed_groups.setdefault(int(label), []).append(cid)
    replayed_partition = [sorted(ids) for ids in replayed_groups.values()]

    identical_partition = not missing_cycle_ids and {frozenset(g) for g in original_partition} == {
        frozenset(g) for g in replayed_partition
    }

    return SchoolsReplayResult(
        run_id=run_id,
        seed=seed,
        k=k,
        identical_partition=identical_partition,
        original_partition=original_partition,
        replayed_partition=replayed_partition,
        missing_cycle_ids=missing_cycle_ids,
    )


@dataclass(frozen=True)
class ComparativeReplayResult:
    cycle_id: int
    baseline_model: str
    variant_model: str
    baseline_fragment: str
    variant_fragment: str
    diff: str


def replay_cycle_level5(cfg: AppConfig, cycle_id: int, variant_model: str) -> ComparativeReplayResult:
    """
    L5 comparative replay: re-runs a cycle's exact stored prompt under BOTH
    its original recorded model (baseline) and a caller-specified variant,
    diffing the two FRESH generations against each other -- not against the
    historically stored fragment, which is L1's job. Scoped to model
    variants only: full alternate ontology/embedding-space comparison would
    require Phase D's bitemporal canon, which does not exist yet.
    """
    with connect(cfg.database) as conn:
        row = conn.execute("SELECT prompt, model FROM cycles WHERE id = ?", (cycle_id,)).fetchone()

    if row is None or not row["prompt"]:
        raise ReplayNotFoundError(f"No cycle {cycle_id} with a stored prompt to replay")

    baseline_model = row["model"]
    baseline_fragment = generate_fragment(cfg, row["prompt"], model=baseline_model)
    variant_fragment = generate_fragment(cfg, row["prompt"], model=variant_model)

    diff = "\n".join(
        difflib.unified_diff(
            baseline_fragment.splitlines(),
            variant_fragment.splitlines(),
            fromfile=f"cycle_{cycle_id}_{baseline_model}",
            tofile=f"cycle_{cycle_id}_{variant_model}",
            lineterm="",
        )
    )

    return ComparativeReplayResult(
        cycle_id=cycle_id,
        baseline_model=baseline_model,
        variant_model=variant_model,
        baseline_fragment=baseline_fragment,
        variant_fragment=variant_fragment,
        diff=diff,
    )


def _canon_rows_for_cycle_ids(cfg: AppConfig, cycle_ids: list[int], limit: int = 3) -> list[dict]:
    """
    The same {"cycle_id", "ref", "content", "path"} shape
    canon.load_canon_fragments produces, but selecting by RECENCY over an
    explicit candidate set rather than MMR-over-embeddings: MMR selection
    depends on the CURRENT embedding index and query relevance, neither of
    which is itself time-travelable in this slice -- an honest scoping
    limit, not a bug. Comparing "canon now" against "canon as of T" this
    way still isolates exactly the variable Phase D's as-of queries
    change (which cycle ids count as valid canon), without also
    conflating it with a different selection algorithm.
    """
    if not cycle_ids:
        return []
    with connect(cfg.database) as conn:
        placeholders = ",".join("?" for _ in cycle_ids)
        rows = conn.execute(
            f"SELECT id, fragment FROM cycles WHERE id IN ({placeholders}) AND fragment IS NOT NULL "
            f"ORDER BY id DESC LIMIT ?",
            (*cycle_ids, limit),
        ).fetchall()
    return [
        {
            "cycle_id": int(row["id"]),
            "ref": f"canon / cycle_{row['id']}",
            "content": (row["fragment"] or "")[:1200],
            "path": f"cycles.id={row['id']}",
        }
        for row in rows
    ]


@dataclass(frozen=True)
class HistoricalCanonReplayResult:
    cycle_id: int
    as_of: str
    current_canon_cycle_ids: list[int]
    historical_canon_cycle_ids: list[int]
    current_canon_fragment: str
    historical_canon_fragment: str
    diff: str


def replay_cycle_against_historical_canon(
    cfg: AppConfig, cycle_id: int, as_of: str, model: str | None = None
) -> HistoricalCanonReplayResult:
    """
    L5-adjacent comparative replay, varying canon STATE rather than model
    (replay_cycle_level5's axis): rebuilds a fresh prompt from the cycle's
    original query and book/json evidence (from cycle_sources, stored
    verbatim), but swaps in the canon pool active as of `as_of`
    (temporal.canon_valid_as_of) instead of canon active right now, then
    diffs two FRESH generations against each other -- same "diff two
    fresh generations, not the historically stored one" discipline
    replay_cycle_level5 established.
    """
    with connect(cfg.database) as conn:
        cycle_row = conn.execute("SELECT query, model FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        if cycle_row is None:
            raise ReplayNotFoundError(f"No cycle {cycle_id}")
        source_rows = conn.execute(
            "SELECT source_kind, ref, content FROM cycle_sources WHERE cycle_id = ? ORDER BY rowid", (cycle_id,)
        ).fetchall()

    book_rows = [
        {"canonical_ref": row["ref"], "content": row["content"], "source_type": "book"}
        for row in source_rows
        if row["source_kind"] == "book"
    ]
    json_rows = [
        {"id": row["ref"], "statement": row["content"], "tradition": "", "category": "", "severity": 0.0, "gloss": ""}
        for row in source_rows
        if row["source_kind"] == "json"
    ]

    replay_model = model or cycle_row["model"]
    query = cycle_row["query"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

    current_canon_ids = canon_valid_as_of(cfg, now)
    historical_canon_ids = canon_valid_as_of(cfg, as_of)

    current_prompt = build_prompt(cfg, query, book_rows, json_rows, _canon_rows_for_cycle_ids(cfg, current_canon_ids))
    historical_prompt = build_prompt(
        cfg, query, book_rows, json_rows, _canon_rows_for_cycle_ids(cfg, historical_canon_ids)
    )

    current_fragment = generate_fragment(cfg, current_prompt, model=replay_model)
    historical_fragment = generate_fragment(cfg, historical_prompt, model=replay_model)

    diff = "\n".join(
        difflib.unified_diff(
            current_fragment.splitlines(),
            historical_fragment.splitlines(),
            fromfile=f"cycle_{cycle_id}_canon_now",
            tofile=f"cycle_{cycle_id}_canon_as_of_{as_of}",
            lineterm="",
        )
    )

    return HistoricalCanonReplayResult(
        cycle_id=cycle_id,
        as_of=as_of,
        current_canon_cycle_ids=current_canon_ids,
        historical_canon_cycle_ids=historical_canon_ids,
        current_canon_fragment=current_fragment,
        historical_canon_fragment=historical_fragment,
        diff=diff,
    )
