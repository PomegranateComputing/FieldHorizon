from __future__ import annotations

import logging

from .config import AppConfig
from .db import connect
from .embeddings import cosine_similarity, load_cycle_embedding
from .fragments import extract_ngrams
from .llm import embed as embed_text

logger = logging.getLogger(__name__)

MOTIF_WINDOW = 20


def _load_canon_pool(cfg: AppConfig, pool_size: int) -> list[dict]:
    with connect(cfg.database) as conn:
        rows = conn.execute(
            """
            SELECT id, fragment
            FROM cycles
            WHERE verdict = 'CANON'
              AND dry_run = 0
              AND fragment IS NOT NULL
              AND fragment != ''
              AND retired_at IS NULL
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (pool_size,),
        ).fetchall()

    return [{"id": int(row["id"]), "fragment": row["fragment"]} for row in rows]


def _select_by_mmr(
    cfg: AppConfig,
    query: str,
    pool: list[dict],
    limit: int,
    lambda_mult: float,
) -> list[dict]:
    """
    Maximal marginal relevance: each pick maximizes relevance to the query
    embedding minus its similarity to fragments already selected, so the
    injected canon is relevant but not a cluster of near-duplicates of
    whichever fragment happens to be newest (review §7's motif-collapse
    warning about recency-only selection).
    """
    try:
        query_vector = embed_text(cfg, query) if query else None
    except Exception as exc:
        logger.warning("Canon MMR: query embedding failed, falling back to recency: %s", exc)
        return pool[:limit]

    vectors: dict[int, list[float]] = {}
    for item in pool:
        vector = load_cycle_embedding(cfg, item["id"])
        if vector is not None:
            vectors[item["id"]] = vector

    if query_vector is None or not vectors:
        logger.warning(
            "Canon MMR: no canon embeddings available yet, falling back to recency-only selection."
        )
        return pool[:limit]

    candidates = [item for item in pool if item["id"] in vectors]
    unembedded = [item for item in pool if item["id"] not in vectors]

    selected: list[dict] = []
    remaining = candidates[:]

    while remaining and len(selected) < limit:

        def mmr_score(item: dict) -> float:
            relevance = cosine_similarity(query_vector, vectors[item["id"]])
            if not selected:
                return relevance
            diversity = max(cosine_similarity(vectors[item["id"]], vectors[s["id"]]) for s in selected)
            return lambda_mult * relevance - (1 - lambda_mult) * diversity

        best = max(remaining, key=mmr_score)
        selected.append(best)
        remaining.remove(best)

    if len(selected) < limit:
        # Pool larger than embedded candidates (e.g. embeddings only just
        # turned on): fill remaining slots by recency, not by omitting them.
        selected.extend(unembedded[: limit - len(selected)])

    return selected


def load_canon_fragments(
    cfg: AppConfig,
    query: str = "",
    limit: int = 3,
    pool_size: int = 30,
    lambda_mult: float = 0.5,
) -> list[dict]:
    """
    The database is the source of truth for canon (review §5): cycles are
    scored and their extracted fragment stored at write time, so recovering
    canon means a query, not regexing the markdown log format.

    Selection is by maximal marginal relevance over the most recent
    `pool_size` canon fragments, not recency alone (review's real-upgrade
    tier §2): `load_canon_fragments(limit=3)` used to always return the
    three newest fragments, which lets whatever motif the last canonization
    happened to contain dominate every subsequent prompt.
    """
    pool = _load_canon_pool(cfg, pool_size)

    if not pool:
        return []

    selected = pool[:limit] if len(pool) <= limit else _select_by_mmr(cfg, query, pool, limit, lambda_mult)

    return [
        {
            "cycle_id": item["id"],
            "ref": f"canon / cycle_{item['id']}",
            "content": item["fragment"][:1200],
            "path": f"cycles.id={item['id']}",
        }
        for item in selected
    ]


def record_canon_ngrams(cfg: AppConfig, cycle_id: int, fragment: str) -> None:
    """
    Store the distinct 3-grams of a newly-canonized fragment (review's
    real-upgrade tier §2), so `load_motif_counts` can compute frequency
    across a recent window without re-tokenizing every canon fragment on
    every evaluation.
    """
    grams = set(extract_ngrams(fragment, n=3))
    if not grams:
        return

    with connect(cfg.database) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO canon_ngrams(cycle_id, ngram) VALUES (?, ?)",
            [(cycle_id, gram) for gram in grams],
        )
        conn.commit()


def load_motif_counts(cfg: AppConfig, window: int = MOTIF_WINDOW) -> dict[str, int]:
    """
    3-gram frequency across the last `window` canon fragments: how many of
    those fragments contain each 3-gram. Windowed by construction (the
    SELECT restricts to the most recent canon cycle_ids) rather than a
    global counter that never forgets a motif from years ago.
    """
    with connect(cfg.database) as conn:
        rows = conn.execute(
            """
            SELECT ngram, COUNT(*) AS freq
            FROM canon_ngrams
            WHERE cycle_id IN (
                SELECT id FROM cycles
                WHERE verdict = 'CANON' AND dry_run = 0
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            )
            GROUP BY ngram
            """,
            (window,),
        ).fetchall()

    return {row["ngram"]: int(row["freq"]) for row in rows}
