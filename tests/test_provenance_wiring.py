from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.cycle import run_cycle
from fieldhorizon.db import connect, init_db
from fieldhorizon.evaluate import CycleEvaluation
from fieldhorizon.provenance import (
    OBJECT_CHUNK,
    OBJECT_CYCLE,
    OBJECT_JSON_ENTRY,
    REL_SELECTED_BY,
    REL_SUPPORTS,
    ProvenanceEdgeRepository,
)
from fieldhorizon.registries import _model_id_cache


@pytest.fixture(autouse=True)
def _clear_model_id_cache():
    _model_id_cache.clear()
    yield
    _model_id_cache.clear()


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


def _evaluation(verdict: str = "HERESY") -> CycleEvaluation:
    return CycleEvaluation(
        symbolic_density=1.0, symbolic_density_method="keyword",
        doctrinal_enforcement=1.0, doctrinal_enforcement_method="keyword",
        length_score=1.0, structure_score=1.0, stuffing_penalty=0.0, generic_penalty=0.0,
        final_score=0.9, verdict=verdict, notes=[],
    )


def _insert_source_and_chunk(cfg: AppConfig, canonical_ref: str, path: str = "/tmp/test.txt") -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO sources(title, path, source_type) VALUES ('Test Source', ?, 'book')",
            (path,),
        )
        source_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
            "VALUES (?, 0, ?, 'chunk content', 10)",
            (source_id, canonical_ref),
        )
        conn.commit()
        return int(cur.lastrowid)


def _insert_json_entry(cfg: AppConfig, entry_id: str) -> None:
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO json_entries(id, group_name, category, statement, raw_json) "
            "VALUES (?, 'g', 'c', 'a statement', '{}')",
            (entry_id,),
        )
        conn.commit()


def _insert_parent_cycle(cfg: AppConfig) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('parent query', 'm', 'p', 'r', 0, 'CANON', 0.9, 'parent fragment')"
        )
        conn.commit()
        return int(cur.lastrowid)


def test_run_cycle_records_selected_by_and_supports_edges(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    chunk_id = _insert_source_and_chunk(cfg, "Test Source, p.1")
    _insert_json_entry(cfg, "axiom_1")
    parent_cycle_id = _insert_parent_cycle(cfg)

    book_rows = [{"canonical_ref": "Test Source, p.1", "content": "chunk content", "source_type": "book"}]
    json_rows = [
        {"id": "axiom_1", "statement": "a statement", "tradition": "synthetic", "category": "c", "severity": 0.5, "gloss": ""}
    ]
    canon_rows = [
        {"cycle_id": parent_cycle_id, "ref": f"canon / cycle_{parent_cycle_id}", "content": "parent fragment", "path": ""}
    ]

    with (
        patch("fieldhorizon.cycle.search_books", return_value=book_rows),
        patch("fieldhorizon.cycle.search_json", return_value=json_rows),
        patch("fieldhorizon.cycle.load_canon_fragments", return_value=canon_rows),
        patch("fieldhorizon.cycle.generate_fragment", return_value="a fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", return_value=_evaluation("HERESY")),
        patch("fieldhorizon.cycle.record_cycle_weather"),
        patch("fieldhorizon.registries.requests.get", side_effect=RuntimeError("no ollama")),
    ):
        cycle_id = run_cycle(cfg, "a query", dry_run=False)

    repo = ProvenanceEdgeRepository(cfg)

    [selected_by] = repo.edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_SELECTED_BY)
    assert selected_by.source_type == OBJECT_CYCLE
    assert selected_by.source_id == str(parent_cycle_id)

    supports = repo.edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_SUPPORTS)
    assert {(e.source_type, e.source_id) for e in supports} == {
        (OBJECT_CHUNK, str(chunk_id)),
        (OBJECT_JSON_ENTRY, "axiom_1"),
    }

    from fieldhorizon.temporal import TemporalCanonRepository

    [state] = TemporalCanonRepository(cfg).history_for_cycle(cycle_id)
    assert state.verdict == "HERESY"
    assert state.active is True
    assert state.source_event_id is not None


def test_run_cycle_skips_an_ambiguous_book_ref_without_raising(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    # Two chunks sharing the same canonical_ref -- resolve_chunk_id must
    # refuse to guess, and the cycle must still complete successfully.
    _insert_source_and_chunk(cfg, "Ambiguous Ref", path="/tmp/test_a.txt")
    _insert_source_and_chunk(cfg, "Ambiguous Ref", path="/tmp/test_b.txt")

    book_rows = [{"canonical_ref": "Ambiguous Ref", "content": "chunk content", "source_type": "book"}]

    with (
        patch("fieldhorizon.cycle.search_books", return_value=book_rows),
        patch("fieldhorizon.cycle.search_json", return_value=[]),
        patch("fieldhorizon.cycle.load_canon_fragments", return_value=[]),
        patch("fieldhorizon.cycle.generate_fragment", return_value="a fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", return_value=_evaluation("HERESY")),
        patch("fieldhorizon.cycle.record_cycle_weather"),
        patch("fieldhorizon.registries.requests.get", side_effect=RuntimeError("no ollama")),
    ):
        cycle_id = run_cycle(cfg, "a query", dry_run=False)

    repo = ProvenanceEdgeRepository(cfg)
    assert repo.edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_SUPPORTS) == []
