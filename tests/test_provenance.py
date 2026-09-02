from __future__ import annotations

from pathlib import Path

from fieldhorizon.config import AppConfig
from fieldhorizon.db import init_db
from fieldhorizon.provenance import (
    CREATION_BACKFILL,
    OBJECT_CYCLE,
    OBJECT_SCHOOL,
    REL_MEMBER_OF,
    REL_SELECTED_BY,
    ProvenanceEdgeRepository,
    object_ref,
)


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


def test_object_ref_uses_the_type_colon_id_convention():
    assert object_ref(OBJECT_SCHOOL, 3) == "school:3"


def test_repository_has_no_update_or_delete_methods():
    public_methods = {name for name in dir(ProvenanceEdgeRepository) if not name.startswith("_")}
    assert public_methods == {"append", "edges_from", "edges_to", "count", "get_by_id"}


def test_append_round_trips_an_edge(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    edge = ProvenanceEdgeRepository(cfg).append(
        OBJECT_CYCLE, 1, OBJECT_CYCLE, 2, REL_SELECTED_BY, evidence_ref="canon / cycle_1"
    )

    assert edge.source_type == OBJECT_CYCLE
    assert edge.source_id == "1"
    assert edge.target_id == "2"
    assert edge.relation_type == REL_SELECTED_BY
    assert edge.confidence == 1.0
    assert edge.evidence_ref == "canon / cycle_1"
    assert edge.id is not None


def test_get_by_id_returns_the_stored_edge(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    edge = ProvenanceEdgeRepository(cfg).append(OBJECT_CYCLE, 1, OBJECT_CYCLE, 2, REL_SELECTED_BY)
    fetched = ProvenanceEdgeRepository(cfg).get_by_id(edge.id)

    assert fetched is not None
    assert fetched.id == edge.id
    assert fetched.source_id == "1"


def test_get_by_id_returns_none_for_an_unknown_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert ProvenanceEdgeRepository(cfg).get_by_id(999) is None


def test_append_returns_the_same_id_when_a_backfill_re_records_the_same_edge(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = ProvenanceEdgeRepository(cfg)

    first = repo.append(OBJECT_CYCLE, 1, OBJECT_CYCLE, 2, REL_SELECTED_BY)
    second = repo.append(OBJECT_CYCLE, 1, OBJECT_CYCLE, 2, REL_SELECTED_BY, creation_method=CREATION_BACKFILL)

    assert first.id == second.id


def test_append_is_idempotent_by_natural_key(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = ProvenanceEdgeRepository(cfg)

    repo.append(OBJECT_CYCLE, 1, OBJECT_CYCLE, 2, REL_SELECTED_BY)
    repo.append(OBJECT_CYCLE, 1, OBJECT_CYCLE, 2, REL_SELECTED_BY, creation_method=CREATION_BACKFILL)

    assert repo.count() == 1


def test_append_first_writer_wins_over_a_later_backfill_attempt(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = ProvenanceEdgeRepository(cfg)

    repo.append(OBJECT_CYCLE, 1, OBJECT_CYCLE, 2, REL_SELECTED_BY, confidence=1.0)
    repo.append(OBJECT_CYCLE, 1, OBJECT_CYCLE, 2, REL_SELECTED_BY, confidence=0.5, creation_method=CREATION_BACKFILL)

    [stored] = repo.edges_from(OBJECT_CYCLE, 1)
    assert stored.confidence == 1.0
    assert stored.creation_method == "write_time"


def test_distinct_relation_types_between_the_same_pair_both_persist(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = ProvenanceEdgeRepository(cfg)

    repo.append(OBJECT_CYCLE, 1, OBJECT_SCHOOL, 1, REL_MEMBER_OF)
    repo.append(OBJECT_CYCLE, 1, OBJECT_SCHOOL, 1, REL_SELECTED_BY)

    assert repo.count() == 2


def test_edges_from_filters_by_relation_type(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = ProvenanceEdgeRepository(cfg)

    repo.append(OBJECT_CYCLE, 1, OBJECT_SCHOOL, 1, REL_MEMBER_OF)
    repo.append(OBJECT_CYCLE, 1, OBJECT_CYCLE, 2, REL_SELECTED_BY)

    only_member_of = repo.edges_from(OBJECT_CYCLE, 1, relation_type=REL_MEMBER_OF)
    assert len(only_member_of) == 1
    assert only_member_of[0].relation_type == REL_MEMBER_OF


def test_edges_to_finds_incoming_edges(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = ProvenanceEdgeRepository(cfg)

    repo.append(OBJECT_CYCLE, 1, OBJECT_SCHOOL, 5, REL_MEMBER_OF)
    repo.append(OBJECT_CYCLE, 2, OBJECT_SCHOOL, 5, REL_MEMBER_OF)

    members = repo.edges_to(OBJECT_SCHOOL, 5, relation_type=REL_MEMBER_OF)
    assert {e.source_id for e in members} == {"1", "2"}


def test_edges_to_returns_empty_for_an_object_with_no_incoming_edges(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert ProvenanceEdgeRepository(cfg).edges_to(OBJECT_SCHOOL, 999) == []
