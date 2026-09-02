from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .config import AppConfig
from .db import connect
from .embeddings import cosine_similarity, current_embedding_version, load_cycle_embedding
from .llm import embed as embed_text
from .ontology_spec import get_ontology
from .storage_vectors import blob_to_vector, vector_to_blob

logger = logging.getLogger(__name__)

SCOPE_CANON = "canon"
SCOPE_SURFACE = "surface"

DEFAULT_SURFACE_LIMIT = 50


def _mean_vector(vectors: list[list[float]]) -> list[float]:
    dim = len(vectors[0])
    return [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]


def compute_axis_vector(cfg: AppConfig, axis_name: str) -> list[float]:
    """
    anchor-axis method: axis_vector = mean(embed(positive anchors)) -
    mean(embed(negative anchors)), normalized. Deterministic given
    ontology.yaml's anchor statements and the current embedding model, so
    it's recomputed only when there is no cached vector for this
    (axis, embedding_version) pair -- a re-pulled embedding model under the
    same tag still gets a fresh vector, same discipline as every other
    embedding cache here.
    """
    axis = get_ontology().weather_axis(axis_name)
    if axis is None:
        raise ValueError(f"Unknown weather axis: {axis_name!r}")

    positive_vectors = [embed_text(cfg, statement) for statement in axis.positive_anchors]
    negative_vectors = [embed_text(cfg, statement) for statement in axis.negative_anchors]

    positive_mean = _mean_vector(positive_vectors)
    negative_mean = _mean_vector(negative_vectors)
    diff = [p - n for p, n in zip(positive_mean, negative_mean, strict=True)]

    return _normalize(diff)


def get_axis_vector(cfg: AppConfig, axis_name: str) -> list[float]:
    """Cached accessor: fetches from weather_axis_vectors, computing and storing on a cache miss."""
    version = current_embedding_version(cfg)

    with connect(cfg.database) as conn:
        row = conn.execute(
            "SELECT vector FROM weather_axis_vectors WHERE axis = ? AND embedding_version = ?",
            (axis_name, version),
        ).fetchone()

    if row is not None:
        return blob_to_vector(row["vector"])

    vector = compute_axis_vector(cfg, axis_name)

    with connect(cfg.database) as conn:
        conn.execute(
            """
            INSERT INTO weather_axis_vectors(axis, embedding_version, vector, dim)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(axis, embedding_version) DO UPDATE SET
                vector = excluded.vector, dim = excluded.dim, computed_at = CURRENT_TIMESTAMP
            """,
            (axis_name, version, vector_to_blob(vector), len(vector)),
        )
        conn.commit()

    return vector


def axis_reading(fragment_vector: list[float], axis_vector: list[float]) -> float:
    return cosine_similarity(fragment_vector, axis_vector)


def _active_canon_cycle_ids(cfg: AppConfig) -> list[int]:
    with connect(cfg.database) as conn:
        rows = conn.execute(
            "SELECT id FROM cycles WHERE verdict = 'CANON' AND dry_run = 0 AND retired_at IS NULL ORDER BY created_at ASC"
        ).fetchall()
    return [int(row["id"]) for row in rows]


def _surface_aggregate(cfg: AppConfig, limit: int) -> dict[str, float]:
    """
    Non-CANON fragments are never persisted to canon_embeddings (that
    table's whole purpose is MMR selection into future prompts, which only
    ever considers CANON), so "surface weather" can't be recomputed live
    from stored vectors the way "canon weather" can. Instead it reads back
    the most recent already-recorded weather_readings rows per axis --
    record_cycle_weather writes one such row per axis after every cycle,
    regardless of verdict, from a transient (unstored) embedding taken at
    that moment.
    """
    readings: dict[str, float] = {}
    for history in axis_history(cfg, SCOPE_SURFACE, limit=limit):
        if history.values:
            readings[history.axis] = sum(history.values) / len(history.values)
    return readings


def corpus_weather(cfg: AppConfig, scope: str = SCOPE_CANON, surface_limit: int = DEFAULT_SURFACE_LIMIT) -> dict[str, float]:
    """
    Current doctrinal climate. scope="canon" ("deep weather") is a live
    aggregate over every active (non-retired) CANON cycle's persisted
    fragment embedding. scope="surface" is the mean of the most recent
    `surface_limit` already-recorded surface readings (see
    _surface_aggregate) -- the raw texture of what's being generated,
    not just what has settled into doctrine. Axes with no data are
    omitted rather than reported as 0.0, which would misleadingly read
    as "neutral."
    """
    if scope == SCOPE_SURFACE:
        return _surface_aggregate(cfg, surface_limit)

    cycle_ids = _active_canon_cycle_ids(cfg)
    vectors = [v for v in (load_cycle_embedding(cfg, cid) for cid in cycle_ids) if v is not None]
    if not vectors:
        return {}

    readings: dict[str, float] = {}
    for axis_name in get_ontology().all_weather_axis_names():
        axis_vector = get_axis_vector(cfg, axis_name)
        readings[axis_name] = sum(axis_reading(v, axis_vector) for v in vectors) / len(vectors)

    return readings


def record_cycle_weather(cfg: AppConfig, cycle_id: int, fragment: str) -> int:
    """
    Called after every cycle, any verdict: embeds `fragment` transiently
    (never stored -- canon_embeddings is reserved for CANON/MMR selection)
    purely to compute this one fragment's own per-axis reading, recorded
    under scope="surface". Read-only: this never feeds back into
    evaluation or generation. Best-effort -- an unreachable embedding
    model degrades to "no surface reading this cycle," not a failed cycle.
    """
    try:
        vector = embed_text(cfg, fragment)
    except Exception as exc:
        logger.warning("Surface weather reading skipped for cycle %d: %s", cycle_id, exc)
        return 0

    readings = {axis: axis_reading(vector, get_axis_vector(cfg, axis)) for axis in get_ontology().all_weather_axis_names()}
    return _write_readings(cfg, SCOPE_SURFACE, readings, cycle_id=cycle_id)


def record_council_weather(cfg: AppConfig, council_id: int) -> int:
    """
    Called after every council: records the current corpus-wide "deep
    weather" (scope="canon") aggregate, since a council is exactly when
    canon composition may have just changed.
    """
    readings = corpus_weather(cfg, scope=SCOPE_CANON)
    return _write_readings(cfg, SCOPE_CANON, readings, council_id=council_id)


def backfill_canon_weather(cfg: AppConfig) -> int:
    """
    One-time (idempotent) reconstruction of canon-scope history: for every
    active CANON cycle with no existing weather_readings row, writes that
    cycle's own per-axis reading backdated to its created_at -- giving the
    dashboard's sparkline real history across canon's growth instead of
    starting empty at whatever moment this phase was turned on.
    """
    with connect(cfg.database) as conn:
        already_backfilled = {
            int(row["cycle_id"])
            for row in conn.execute(
                "SELECT DISTINCT cycle_id FROM weather_readings WHERE scope = ? AND cycle_id IS NOT NULL", (SCOPE_CANON,)
            ).fetchall()
        }

    count = 0
    for cycle_id in _active_canon_cycle_ids(cfg):
        if cycle_id in already_backfilled:
            continue
        vector = load_cycle_embedding(cfg, cycle_id)
        if vector is None:
            continue

        with connect(cfg.database) as conn:
            created_at = conn.execute("SELECT created_at FROM cycles WHERE id = ?", (cycle_id,)).fetchone()["created_at"]

        readings = {axis: axis_reading(vector, get_axis_vector(cfg, axis)) for axis in get_ontology().all_weather_axis_names()}
        count += _write_readings(cfg, SCOPE_CANON, readings, cycle_id=cycle_id, recorded_at=created_at)

    return count


def _write_readings(
    cfg: AppConfig,
    scope: str,
    readings: dict[str, float],
    cycle_id: int | None = None,
    council_id: int | None = None,
    recorded_at: str | None = None,
) -> int:
    if not readings:
        return 0

    with connect(cfg.database) as conn:
        for axis, value in readings.items():
            if recorded_at is not None:
                conn.execute(
                    "INSERT INTO weather_readings(recorded_at, scope, axis, value, cycle_id, council_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (recorded_at, scope, axis, value, cycle_id, council_id),
                )
            else:
                conn.execute(
                    "INSERT INTO weather_readings(scope, axis, value, cycle_id, council_id) VALUES (?, ?, ?, ?, ?)",
                    (scope, axis, value, cycle_id, council_id),
                )
        conn.commit()

    return len(readings)


def school_weather_profile(cfg: AppConfig, cycle_ids: list[int]) -> dict[str, float]:
    """Same per-axis aggregate as corpus_weather's canon scope, scoped to one school's member cycles."""
    vectors = [v for v in (load_cycle_embedding(cfg, cid) for cid in cycle_ids) if v is not None]
    if not vectors:
        return {}

    profile: dict[str, float] = {}
    for axis_name in get_ontology().all_weather_axis_names():
        axis_vector = get_axis_vector(cfg, axis_name)
        profile[axis_name] = sum(axis_reading(v, axis_vector) for v in vectors) / len(vectors)
    return profile


@dataclass(frozen=True)
class AxisHistory:
    axis: str
    values: list[float]  # oldest -> newest


def axis_history(cfg: AppConfig, scope: str, limit: int = 30) -> list[AxisHistory]:
    """Most recent `limit` readings per axis, oldest-first, for a dashboard sparkline."""
    histories: list[AxisHistory] = []
    with connect(cfg.database) as conn:
        for axis_name in get_ontology().all_weather_axis_names():
            rows = conn.execute(
                "SELECT value FROM weather_readings WHERE scope = ? AND axis = ? ORDER BY recorded_at DESC LIMIT ?",
                (scope, axis_name, limit),
            ).fetchall()
            values = [float(row["value"]) for row in reversed(rows)]
            histories.append(AxisHistory(axis=axis_name, values=values))
    return histories
