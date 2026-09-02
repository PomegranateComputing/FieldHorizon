from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fieldhorizon.concepts import (
    semantic_neighbors,
    store_chunk_tags,
    tag_chunk_content,
    tag_chunks,
    top_semantic_nodes,
)
from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db

DOMAIN_NAMES = ("tawhid", "technology", "apocalypse")


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


_source_counter = 0


def insert_chunk(cfg: AppConfig, content: str) -> int:
    global _source_counter
    _source_counter += 1
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO sources(title, path, source_type) VALUES ('t', ?, 'book')",
            (f"p{_source_counter}",),
        )
        source_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
            "VALUES (?, 0, ?, ?, 1)",
            (source_id, f"ref{_source_counter}", content),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_tag_chunk_content_rejects_hallucinated_domain(tmp_path):
    raw = json.dumps({
        "domains": [
            {"domain": "tawhid", "confidence": 0.9},
            {"domain": "made_up_domain_that_does_not_exist", "confidence": 0.9},
        ],
        "entities": [],
        "motifs": [],
    })
    cfg = make_config(tmp_path)
    with patch("fieldhorizon.concepts.call_ollama", return_value=raw):
        tags = tag_chunk_content(cfg, "some content", DOMAIN_NAMES)

    domain_names = {d["domain"] for d in tags["domains"]}
    assert domain_names == {"tawhid"}
    assert "made_up_domain_that_does_not_exist" not in domain_names


def test_tag_chunk_content_clamps_confidence_to_unit_interval(tmp_path):
    raw = json.dumps({
        "domains": [{"domain": "tawhid", "confidence": 5.0}],
        "entities": [],
        "motifs": [],
    })
    cfg = make_config(tmp_path)
    with patch("fieldhorizon.concepts.call_ollama", return_value=raw):
        tags = tag_chunk_content(cfg, "content", DOMAIN_NAMES)

    assert tags["domains"][0]["confidence"] == 1.0


def test_tag_chunk_content_rejects_invalid_entity_kind(tmp_path):
    raw = json.dumps({
        "domains": [],
        "entities": [
            {"name": "Al-Ghazali", "kind": "person"},
            {"name": "Nonsense", "kind": "not_a_real_kind"},
        ],
        "motifs": [],
    })
    cfg = make_config(tmp_path)
    with patch("fieldhorizon.concepts.call_ollama", return_value=raw):
        tags = tag_chunk_content(cfg, "content", DOMAIN_NAMES)

    names = {e["name"] for e in tags["entities"]}
    assert names == {"Al-Ghazali"}


def test_tag_chunk_content_caps_motifs_at_three(tmp_path):
    raw = json.dumps({
        "domains": [],
        "entities": [],
        "motifs": ["one", "two", "three", "four", "five"],
    })
    cfg = make_config(tmp_path)
    with patch("fieldhorizon.concepts.call_ollama", return_value=raw):
        tags = tag_chunk_content(cfg, "content", DOMAIN_NAMES)

    assert len(tags["motifs"]) == 3


def test_store_chunk_tags_writes_all_three_tables_and_marks_chunk_tagged(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    chunk_id = insert_chunk(cfg, "content")

    tags = {
        "domains": [{"domain": "tawhid", "confidence": 0.8}],
        "entities": [{"name": "Al-Ghazali", "kind": "person"}],
        "motifs": ["the veiled face"],
    }
    store_chunk_tags(cfg, chunk_id, tags)

    with connect(cfg.database) as conn:
        concepts = conn.execute("SELECT * FROM chunk_concepts WHERE chunk_id = ?", (chunk_id,)).fetchall()
        entities = conn.execute("SELECT * FROM chunk_entities WHERE chunk_id = ?", (chunk_id,)).fetchall()
        motifs = conn.execute("SELECT * FROM chunk_motifs WHERE chunk_id = ?", (chunk_id,)).fetchall()
        tagged_at = conn.execute("SELECT concepts_tagged_at FROM chunks WHERE id = ?", (chunk_id,)).fetchone()

    assert len(concepts) == 1 and concepts[0]["domain"] == "tawhid"
    assert len(entities) == 1 and entities[0]["entity"] == "Al-Ghazali"
    assert len(motifs) == 1 and motifs[0]["motif"] == "the veiled face"
    assert tagged_at["concepts_tagged_at"] is not None


def test_tag_chunks_is_resumable_and_skips_already_tagged_chunks(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    already_tagged_id = insert_chunk(cfg, "already tagged content")
    store_chunk_tags(cfg, already_tagged_id, {"domains": [], "entities": [], "motifs": []})
    untagged_id = insert_chunk(cfg, "untagged content")

    raw = json.dumps({"domains": [], "entities": [], "motifs": ["a motif"]})
    with patch("fieldhorizon.concepts.call_ollama", return_value=raw) as mock_call:
        count = tag_chunks(cfg, show_progress=False)

    assert count == 1
    mock_call.assert_called_once()
    with connect(cfg.database) as conn:
        tagged = conn.execute(
            "SELECT id FROM chunks WHERE concepts_tagged_at IS NOT NULL ORDER BY id"
        ).fetchall()
    assert {row["id"] for row in tagged} == {already_tagged_id, untagged_id}


def test_tag_chunks_respects_max_chunks_budget(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    for i in range(3):
        insert_chunk(cfg, f"content {i}")

    raw = json.dumps({"domains": [], "entities": [], "motifs": []})
    with patch("fieldhorizon.concepts.call_ollama", return_value=raw):
        count = tag_chunks(cfg, max_chunks=2, show_progress=False)

    assert count == 2


def test_tag_chunks_skips_a_chunk_whose_tagging_call_fails(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    insert_chunk(cfg, "content")

    with patch("fieldhorizon.concepts.call_ollama", side_effect=RuntimeError("no model")):
        count = tag_chunks(cfg, show_progress=False)

    assert count == 0
    with connect(cfg.database) as conn:
        row = conn.execute("SELECT concepts_tagged_at FROM chunks").fetchone()
    assert row["concepts_tagged_at"] is None


def _seed_cooccurrence_fixture(cfg: AppConfig) -> None:
    """Three chunks: A/B share domain=tawhid and entity=moses; B/C share motif=the_machine; only technology never co-occurs with tawhid."""
    chunk_a = insert_chunk(cfg, "a")
    chunk_b = insert_chunk(cfg, "b")
    chunk_c = insert_chunk(cfg, "c")
    store_chunk_tags(
        cfg, chunk_a,
        {"domains": [{"domain": "tawhid", "confidence": 1.0}], "entities": [{"name": "moses", "kind": "person"}], "motifs": ["the_veil"]},
    )
    store_chunk_tags(
        cfg, chunk_b,
        {"domains": [{"domain": "tawhid", "confidence": 1.0}], "entities": [{"name": "moses", "kind": "person"}], "motifs": ["the_machine"]},
    )
    store_chunk_tags(
        cfg, chunk_c,
        {"domains": [{"domain": "technology", "confidence": 1.0}], "entities": [], "motifs": ["the_machine"]},
    )


def test_top_semantic_nodes_orders_by_distinct_chunk_count(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _seed_cooccurrence_fixture(cfg)

    nodes = top_semantic_nodes(cfg, "domain")
    assert [(n.label, n.count) for n in nodes] == [("tawhid", 2), ("technology", 1)]


def test_semantic_neighbors_finds_cross_kind_cooccurrence(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _seed_cooccurrence_fixture(cfg)

    result = semantic_neighbors(cfg, "domain", "tawhid")
    labels = {(n.kind, n.label): n.shared_chunk_count for n in result.neighbors}
    assert labels[("entity", "moses")] == 2
    assert labels[("motif", "the_veil")] == 1
    assert labels[("motif", "the_machine")] == 1
    # technology never shares a chunk with tawhid -- must not appear at all.
    assert ("domain", "technology") not in labels


def test_semantic_neighbors_never_includes_itself(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _seed_cooccurrence_fixture(cfg)

    result = semantic_neighbors(cfg, "domain", "tawhid")
    assert ("domain", "tawhid") not in {(n.kind, n.label) for n in result.neighbors}


def test_semantic_neighbors_respects_the_limit_but_reports_the_true_total(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _seed_cooccurrence_fixture(cfg)

    result = semantic_neighbors(cfg, "domain", "tawhid", limit=1)
    assert len(result.neighbors) == 1
    assert result.total_neighbor_count == 3


def test_semantic_neighbors_is_empty_for_an_unknown_label(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    result = semantic_neighbors(cfg, "domain", "nonexistent_domain")
    assert result.neighbors == []
    assert result.total_neighbor_count == 0
