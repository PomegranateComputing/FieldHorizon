from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .config import AppConfig
from .db import connect
from .embeddings import cosine_similarity, current_embedding_version
from .storage_vectors import VectorStore, blob_to_vector, vector_to_blob

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceFingerprint:
    source_id: int
    embedding_version: str
    mean_vector: list[float]
    domain_distribution: dict[str, float]
    top_motifs: list[str]
    chunk_count: int


def compute_fingerprint(cfg: AppConfig, source_id: int) -> SourceFingerprint | None:
    """
    Mean chunk embedding + domain distribution (each domain's share of
    total confidence-weight across this source's tagged chunks) + top
    motifs (by frequency), at the current embedding_version. Returns None
    if this source has no chunk with a current-version vector yet (needs
    backfill_chunk_embeddings first) -- there is nothing meaningful to
    average.
    """
    with connect(cfg.database) as conn:
        chunk_ids = [
            int(row["id"])
            for row in conn.execute("SELECT id FROM chunks WHERE source_id = ?", (source_id,)).fetchall()
        ]

    if not chunk_ids:
        return None

    version = current_embedding_version(cfg)
    vectors = VectorStore(cfg, "chunk").vectors_for_keys(chunk_ids, version)
    if not vectors:
        return None

    dim = len(next(iter(vectors.values())))
    mean_vector = [sum(v[i] for v in vectors.values()) / len(vectors) for i in range(dim)]

    with connect(cfg.database) as conn:
        placeholders = ",".join("?" for _ in chunk_ids)
        concept_rows = conn.execute(
            f"SELECT domain, confidence FROM chunk_concepts WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        ).fetchall()
        motif_rows = conn.execute(
            f"SELECT motif, COUNT(*) AS n FROM chunk_motifs WHERE chunk_id IN ({placeholders}) "
            "GROUP BY motif ORDER BY n DESC, motif ASC LIMIT 10",
            chunk_ids,
        ).fetchall()

    domain_weight: dict[str, float] = {}
    for row in concept_rows:
        domain_weight[row["domain"]] = domain_weight.get(row["domain"], 0.0) + row["confidence"]
    total_weight = sum(domain_weight.values())
    domain_distribution = (
        {domain: weight / total_weight for domain, weight in domain_weight.items()} if total_weight > 0 else {}
    )

    return SourceFingerprint(
        source_id=source_id,
        embedding_version=version,
        mean_vector=mean_vector,
        domain_distribution=domain_distribution,
        top_motifs=[row["motif"] for row in motif_rows],
        chunk_count=len(vectors),
    )


def store_fingerprint(cfg: AppConfig, fp: SourceFingerprint) -> None:
    with connect(cfg.database) as conn:
        conn.execute(
            """
            INSERT INTO source_fingerprints(
                source_id, embedding_version, mean_vector, dim, domain_distribution, top_motifs, chunk_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, embedding_version) DO UPDATE SET
                mean_vector = excluded.mean_vector, dim = excluded.dim,
                domain_distribution = excluded.domain_distribution,
                top_motifs = excluded.top_motifs, chunk_count = excluded.chunk_count,
                computed_at = CURRENT_TIMESTAMP
            """,
            (
                fp.source_id, fp.embedding_version, vector_to_blob(fp.mean_vector), len(fp.mean_vector),
                json.dumps(fp.domain_distribution), json.dumps(fp.top_motifs), fp.chunk_count,
            ),
        )
        conn.commit()


def load_fingerprint(cfg: AppConfig, source_id: int) -> SourceFingerprint | None:
    version = current_embedding_version(cfg)
    with connect(cfg.database) as conn:
        row = conn.execute(
            "SELECT * FROM source_fingerprints WHERE source_id = ? AND embedding_version = ?",
            (source_id, version),
        ).fetchone()

    if row is None:
        return None

    return SourceFingerprint(
        source_id=source_id,
        embedding_version=version,
        mean_vector=blob_to_vector(row["mean_vector"]),
        domain_distribution=json.loads(row["domain_distribution"]),
        top_motifs=json.loads(row["top_motifs"]),
        chunk_count=int(row["chunk_count"]),
    )


def get_or_compute_fingerprint(cfg: AppConfig, source_id: int) -> SourceFingerprint | None:
    """Cached in source_fingerprints, keyed by embedding_version like every other vector table here."""
    fp = load_fingerprint(cfg, source_id)
    if fp is not None:
        return fp

    fp = compute_fingerprint(cfg, source_id)
    if fp is not None:
        store_fingerprint(cfg, fp)
    return fp


def domain_distribution_divergence(a: dict[str, float], b: dict[str, float]) -> float:
    """
    Total variation distance over the union of domains: 0.0 means
    identical distributions, 1.0 means disjoint. Symmetric and bounded,
    which is all a comparison table needs -- no log terms, unlike
    Jensen-Shannon or KL divergence.
    """
    domains = set(a) | set(b)
    if not domains:
        return 0.0
    return sum(abs(a.get(d, 0.0) - b.get(d, 0.0)) for d in domains) / 2.0


@dataclass(frozen=True)
class FingerprintComparison:
    fingerprint_a: SourceFingerprint
    fingerprint_b: SourceFingerprint
    cosine_similarity: float
    domain_divergence: float


def compare_fingerprints(cfg: AppConfig, source_id_a: int, source_id_b: int) -> FingerprintComparison:
    fp_a = get_or_compute_fingerprint(cfg, source_id_a)
    fp_b = get_or_compute_fingerprint(cfg, source_id_b)
    if fp_a is None or fp_b is None:
        missing = source_id_a if fp_a is None else source_id_b
        raise ValueError(f"source {missing} has no embedded chunks yet -- run backfill-chunk-embeddings first")

    return FingerprintComparison(
        fingerprint_a=fp_a,
        fingerprint_b=fp_b,
        cosine_similarity=cosine_similarity(fp_a.mean_vector, fp_b.mean_vector),
        domain_divergence=domain_distribution_divergence(fp_a.domain_distribution, fp_b.domain_distribution),
    )
