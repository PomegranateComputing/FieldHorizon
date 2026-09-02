from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.fingerprint import (
    compare_fingerprints,
    compute_fingerprint,
    domain_distribution_divergence,
    get_or_compute_fingerprint,
    load_fingerprint,
    store_fingerprint,
)
from fieldhorizon.storage_vectors import VectorStore


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        root=tmp_path,
        database=tmp_path / "data.sqlite3",
        books=tmp_path / "books",
        json_corpus=tmp_path / "json_corpus",
        outputs=tmp_path / "outputs",
        logs=tmp_path / "logs",
        ollama_base_url="http://localhost:11434",
        default_model="hermes3:8b",
        temperature=1.25,
        top_p=0.95,
        repeat_penalty=1.08,
        num_ctx=8192,
        book_fragments=6,
        json_entries=8,
        chunk_chars=1800,
        chunk_overlap=250,
        tone="dark",
        mode="canonical_synthesis",
        manifestos=tmp_path / "manifestos",
        embedding_model="nomic-embed-text",
    )


def _fixed_version():
    return patch("fieldhorizon.fingerprint.current_embedding_version", return_value="nomic-embed-text")


def make_source_with_chunks(cfg: AppConfig, path: str, vectors: list[list[float]], domains: list[str | None]) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('t', ?, 'book')", (path,))
        source_id = cur.lastrowid
        chunk_ids = []
        for i, _ in enumerate(vectors):
            cur = conn.execute(
                "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
                "VALUES (?, ?, ?, 'content', 1)",
                (source_id, i, f"{path}-{i}"),
            )
            chunk_ids.append(int(cur.lastrowid))
        conn.commit()

    store = VectorStore(cfg, "chunk", backend="blob")
    for chunk_id, vector in zip(chunk_ids, vectors, strict=True):
        store.upsert(chunk_id, vector, "nomic-embed-text")

    with connect(cfg.database) as conn:
        for chunk_id, domain in zip(chunk_ids, domains, strict=True):
            if domain is not None:
                conn.execute(
                    "INSERT INTO chunk_concepts(chunk_id, domain, confidence) VALUES (?, ?, 1.0)",
                    (chunk_id, domain),
                )
                conn.execute("INSERT INTO chunk_motifs(chunk_id, motif) VALUES (?, ?)", (chunk_id, f"motif-{domain}"))
        conn.commit()

    return int(source_id)


def test_compute_fingerprint_returns_none_for_source_with_no_embedded_chunks(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('t', 'p', 'book')")
        source_id = cur.lastrowid
        conn.commit()

    with _fixed_version():
        assert compute_fingerprint(cfg, source_id) is None


def test_compute_fingerprint_averages_vectors_and_domain_distribution(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    source_id = make_source_with_chunks(
        cfg, "p1", [[1.0, 0.0], [0.0, 1.0]], ["tawhid", "technology"]
    )

    with _fixed_version():
        fp = compute_fingerprint(cfg, source_id)

    assert fp is not None
    assert fp.chunk_count == 2
    assert abs(fp.mean_vector[0] - 0.5) < 1e-6
    assert abs(fp.mean_vector[1] - 0.5) < 1e-6
    assert fp.domain_distribution == {"tawhid": 0.5, "technology": 0.5}
    assert set(fp.top_motifs) == {"motif-tawhid", "motif-technology"}


def test_fingerprint_store_and_load_roundtrips(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    source_id = make_source_with_chunks(cfg, "p1", [[1.0, 0.0]], ["tawhid"])

    with _fixed_version():
        fp = compute_fingerprint(cfg, source_id)
        assert fp is not None
        store_fingerprint(cfg, fp)
        loaded = load_fingerprint(cfg, source_id)

    assert loaded is not None
    assert loaded.source_id == fp.source_id
    assert loaded.embedding_version == fp.embedding_version
    assert loaded.domain_distribution == fp.domain_distribution
    assert loaded.top_motifs == fp.top_motifs
    assert loaded.chunk_count == fp.chunk_count
    assert all(abs(a - b) < 1e-6 for a, b in zip(loaded.mean_vector, fp.mean_vector, strict=True))


def test_get_or_compute_fingerprint_computes_once_then_reuses_cache(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    source_id = make_source_with_chunks(cfg, "p1", [[1.0, 0.0]], ["tawhid"])

    with connect(cfg.database) as conn:
        chunk_id = conn.execute(
            "SELECT id FROM chunks WHERE source_id = ?", (source_id,)
        ).fetchone()["id"]

    with _fixed_version():
        first = get_or_compute_fingerprint(cfg, source_id)
        assert first is not None

        # Mutate the underlying chunk vector directly; get_or_compute must
        # still return the CACHED fingerprint (source_fingerprints row),
        # not recompute -- this is the cache-hit path.
        VectorStore(cfg, "chunk", backend="blob").upsert(chunk_id, [0.0, 1.0], "nomic-embed-text")
        second = get_or_compute_fingerprint(cfg, source_id)

    assert second is not None
    assert all(abs(a - b) < 1e-6 for a, b in zip(first.mean_vector, second.mean_vector, strict=True))


def test_domain_distribution_divergence_identical_is_zero():
    a = {"tawhid": 0.5, "technology": 0.5}
    assert domain_distribution_divergence(a, a) == 0.0


def test_domain_distribution_divergence_disjoint_is_one():
    a = {"tawhid": 1.0}
    b = {"technology": 1.0}
    assert domain_distribution_divergence(a, b) == 1.0


def test_compare_fingerprints_reports_similarity_and_divergence(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    source_a = make_source_with_chunks(cfg, "pa", [[1.0, 0.0]], ["tawhid"])
    source_b = make_source_with_chunks(cfg, "pb", [[1.0, 0.0]], ["tawhid"])

    with _fixed_version():
        comparison = compare_fingerprints(cfg, source_a, source_b)

    assert abs(comparison.cosine_similarity - 1.0) < 1e-6
    assert comparison.domain_divergence == 0.0


def test_compare_fingerprints_raises_when_a_source_has_no_vectors(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    source_a = make_source_with_chunks(cfg, "pa", [[1.0, 0.0]], ["tawhid"])
    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('t', 'pb', 'book')")
        source_b = cur.lastrowid
        conn.commit()

    with _fixed_version(), pytest.raises(ValueError):
        compare_fingerprints(cfg, source_a, source_b)
