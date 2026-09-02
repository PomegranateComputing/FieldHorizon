from __future__ import annotations

import json
from pathlib import Path

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.ingest import ingest_json_corpus
from fieldhorizon.provenance import (
    OBJECT_CHUNK,
    OBJECT_CYCLE,
    OBJECT_JSON_ENTRY,
    REL_DERIVED_FROM,
    REL_EXTRACTED_FROM,
    REL_MUTATES,
    REL_OPPOSES,
    ProvenanceEdgeRepository,
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


def _insert_axiom(cfg: AppConfig, entry_id: str) -> None:
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO json_entries(id, group_name, category, statement, raw_json) VALUES (?, 'g', 'c', 's', '{}')",
            (entry_id,),
        )
        conn.commit()


def _insert_cycle(cfg: AppConfig, cycle_id: int) -> None:
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycles(id, query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES (?, 'q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'f')",
            (cycle_id,),
        )
        conn.commit()


def _write_generated_axiom_file(cfg: AppConfig, provenance: dict) -> None:
    cfg.json_corpus.mkdir(parents=True, exist_ok=True)
    (cfg.json_corpus / "generated_axioms_0001.json").write_text(
        json.dumps(
            [
                {
                    "id": "generated_axiom_0001_0001",
                    "group_name": "generated_axioms",
                    "category": "recursive_axiom",
                    "statement": "a generated statement",
                    "gloss": "generated from pressure",
                    "provenance": provenance,
                }
            ]
        ),
        encoding="utf-8",
    )


def test_ingest_writes_mutates_and_opposes_edges_for_a_generated_axiom(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_axiom(cfg, "src_1")
    _insert_axiom(cfg, "tgt_1")
    _write_generated_axiom_file(
        cfg,
        {
            "source_edges": [
                {
                    "source_id": "src_1", "target_id": "tgt_1",
                    "source_category": "a", "target_category": "b",
                    "pressure_score": 1.2, "reason": "test reason",
                }
            ],
            "stratum": 1,
            "source_cycle_ids": [],
        },
    )

    ingest_json_corpus(cfg)

    repo = ProvenanceEdgeRepository(cfg)
    mutates = repo.edges_from(OBJECT_JSON_ENTRY, "generated_axiom_0001_0001", relation_type=REL_MUTATES)
    assert {e.target_id for e in mutates} == {"src_1", "tgt_1"}

    [opposes] = repo.edges_from(OBJECT_JSON_ENTRY, "src_1", relation_type=REL_OPPOSES)
    assert opposes.target_id == "tgt_1"
    assert opposes.confidence == 1.0  # pressure_score 1.2 saturates at the confidence ceiling
    assert opposes.polarity == -1.0
    assert opposes.evidence_ref == "test reason"


def test_ingest_writes_derived_from_edges_for_source_cycle_ids(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_axiom(cfg, "src_1")
    _insert_axiom(cfg, "tgt_1")
    _insert_cycle(cfg, 7)
    _write_generated_axiom_file(
        cfg,
        {
            "source_edges": [
                {"source_id": "src_1", "target_id": "tgt_1", "source_category": "a", "target_category": "b",
                 "pressure_score": 0.5, "reason": "r"}
            ],
            "stratum": 1,
            "source_cycle_ids": [7],
        },
    )

    ingest_json_corpus(cfg)

    [derived] = ProvenanceEdgeRepository(cfg).edges_from(
        OBJECT_JSON_ENTRY, "generated_axiom_0001_0001", relation_type=REL_DERIVED_FROM
    )
    assert derived.target_type == OBJECT_CYCLE
    assert derived.target_id == "7"


def test_ingest_writes_no_edges_for_a_hand_authored_axiom_without_provenance(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cfg.json_corpus.mkdir(parents=True, exist_ok=True)
    (cfg.json_corpus / "hand_authored.json").write_text(
        json.dumps([{"id": "hand_1", "statement": "a hand-authored axiom"}]), encoding="utf-8"
    )

    ingest_json_corpus(cfg)

    assert ProvenanceEdgeRepository(cfg).count() == 0


def test_ingest_is_idempotent_and_does_not_duplicate_edges_on_a_second_ingest(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_axiom(cfg, "src_1")
    _insert_axiom(cfg, "tgt_1")
    _write_generated_axiom_file(
        cfg,
        {
            "source_edges": [
                {"source_id": "src_1", "target_id": "tgt_1", "source_category": "a", "target_category": "b",
                 "pressure_score": 0.5, "reason": "r"}
            ],
            "stratum": 1,
            "source_cycle_ids": [],
        },
    )

    ingest_json_corpus(cfg)
    first_count = ProvenanceEdgeRepository(cfg).count()
    ingest_json_corpus(cfg)
    second_count = ProvenanceEdgeRepository(cfg).count()

    assert first_count == second_count > 0


def test_ingest_writes_an_extracted_from_edge_when_raw_material_resolves_to_a_chunk(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    raw_sentence = "The machine judges without mercy or memory."
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO sources(title, path, source_type) VALUES ('Test Source', '/tmp/test.txt', 'book')"
        )
        source_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate, char_start) "
            "VALUES (?, 0, 'ref-1', ?, 10, 100)",
            (source_id, f"Some preamble. {raw_sentence} Some epilogue."),
        )
        chunk_id = int(cur.lastrowid)
        conn.commit()

    cfg.json_corpus.mkdir(parents=True, exist_ok=True)
    (cfg.json_corpus / "distilled.json").write_text(
        json.dumps(
            [
                {
                    "id": "distilled_1",
                    "statement": "a distilled axiom",
                    "gloss": f"Distilled from raw polemical sentence: {raw_sentence}",
                }
            ]
        ),
        encoding="utf-8",
    )

    ingest_json_corpus(cfg)

    [extracted] = ProvenanceEdgeRepository(cfg).edges_from(
        OBJECT_JSON_ENTRY, "distilled_1", relation_type=REL_EXTRACTED_FROM
    )
    assert extracted.target_type == OBJECT_CHUNK
    assert extracted.target_id == str(chunk_id)
