from __future__ import annotations

from pathlib import Path

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.storage_vectors import VectorStore, blob_to_vector, vector_to_blob


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


def insert_source_and_chunks(cfg: AppConfig, n: int) -> list[int]:
    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('t', 'p', 'book')")
        source_id = cur.lastrowid
        ids = []
        for i in range(n):
            cur = conn.execute(
                "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
                "VALUES (?, ?, ?, ?, 1)",
                (source_id, i, f"ref-{i}", f"content {i}"),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
    return ids


def test_vector_blob_roundtrip():
    vector = [0.1, -2.5, 3.333333, 0.0]
    restored = blob_to_vector(vector_to_blob(vector))
    assert all(abs(a - b) < 1e-6 for a, b in zip(vector, restored, strict=True))


def test_upsert_and_get_roundtrips_for_both_backends(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    chunk_id = insert_source_and_chunks(cfg, 1)[0]

    for backend in ("blob", "vec0"):
        store = VectorStore(cfg, "chunk", backend=backend)
        store.upsert(chunk_id, [1.0, 2.0, 3.0], "v1")
        got = store.get(chunk_id, "v1")
        assert got is not None
        assert all(abs(a - b) < 1e-6 for a, b in zip(got, [1.0, 2.0, 3.0], strict=True))


def test_get_returns_none_for_stale_version(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    chunk_id = insert_source_and_chunks(cfg, 1)[0]

    store = VectorStore(cfg, "chunk", backend="blob")
    store.upsert(chunk_id, [1.0, 2.0], "v1")

    assert store.get(chunk_id, "v2") is None


def test_missing_keys_identifies_unembedded_rows(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    ids = insert_source_and_chunks(cfg, 3)

    store = VectorStore(cfg, "chunk", backend="blob")
    store.upsert(ids[0], [1.0, 0.0], "v1")

    missing = store.missing_keys(ids, "v1")
    assert missing == [ids[1], ids[2]]


def test_missing_keys_treats_stale_version_as_missing(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    ids = insert_source_and_chunks(cfg, 1)

    store = VectorStore(cfg, "chunk", backend="blob")
    store.upsert(ids[0], [1.0, 0.0], "old-version")

    assert store.missing_keys(ids, "new-version") == ids


def test_upsert_overwrites_existing_vector_for_both_backends(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    chunk_id = insert_source_and_chunks(cfg, 1)[0]

    for backend in ("blob", "vec0"):
        store = VectorStore(cfg, "chunk", backend=backend)
        store.upsert(chunk_id, [1.0, 0.0], "v1")
        store.upsert(chunk_id, [0.0, 1.0], "v1")
        got = store.get(chunk_id, "v1")
        assert got is not None
        assert abs(got[0]) < 1e-6
        assert abs(got[1] - 1.0) < 1e-6


def test_search_ranks_by_cosine_similarity_for_both_backends(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    ids = insert_source_and_chunks(cfg, 2)

    for backend in ("blob", "vec0"):
        store = VectorStore(cfg, "chunk", backend=backend)
        store.upsert(ids[0], [1.0, 0.0], "v1")
        store.upsert(ids[1], [0.0, 1.0], "v1")

        results = store.search([1.0, 0.0], "v1", limit=2)
        assert results[0][0] == ids[0]
        assert results[0][1] > results[1][1]


def test_search_excludes_keys(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    ids = insert_source_and_chunks(cfg, 2)

    store = VectorStore(cfg, "chunk", backend="blob")
    store.upsert(ids[0], [1.0, 0.0], "v1")
    store.upsert(ids[1], [0.9, 0.1], "v1")

    results = store.search([1.0, 0.0], "v1", limit=2, exclude_keys={ids[0]})
    assert [key for key, _ in results] == [ids[1]]


def test_search_filters_out_stale_version_rows(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    ids = insert_source_and_chunks(cfg, 2)

    for backend in ("blob", "vec0"):
        store = VectorStore(cfg, "chunk", backend=backend)
        store.upsert(ids[0], [1.0, 0.0], "old")
        store.upsert(ids[1], [1.0, 0.0], "new")

        results = store.search([1.0, 0.0], "new", limit=5)
        assert [key for key, _ in results] == [ids[1]]


def test_vec0_backend_migrates_existing_blob_rows(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    ids = insert_source_and_chunks(cfg, 2)

    blob_store = VectorStore(cfg, "chunk", backend="blob")
    blob_store.upsert(ids[0], [1.0, 0.0], "v1")
    blob_store.upsert(ids[1], [0.0, 1.0], "v1")

    vec_store = VectorStore(cfg, "chunk", backend="vec0")
    assert vec_store.get(ids[0], "v1") is not None
    assert vec_store.get(ids[1], "v1") is not None
    results = vec_store.search([1.0, 0.0], "v1", limit=2)
    assert results[0][0] == ids[0]


def test_axiom_family_uses_text_keys(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO json_entries(id, group_name, category, tradition, statement, raw_json) "
            "VALUES ('axiom-id-1', 'g', 'c', 't', 's', '{}')"
        )
        conn.commit()

    for backend in ("blob", "vec0"):
        store = VectorStore(cfg, "axiom", backend=backend)
        store.upsert("axiom-id-1", [1.0, 0.0], "v1")
        got = store.get("axiom-id-1", "v1")
        assert got is not None
