from __future__ import annotations

import json

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.lineage import LineageNotFoundError, build_lineage_tree, extract_raw_material


def make_config(tmp_path) -> AppConfig:
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


def insert_entry(cfg: AppConfig, entry_id: str, category: str, statement: str, gloss: str, raw_json: dict) -> None:
    with connect(cfg.database) as conn:
        conn.execute(
            """
            INSERT INTO json_entries(id, group_name, category, statement, gloss, raw_json)
            VALUES (?, 'g', ?, ?, ?, ?)
            """,
            (entry_id, category, statement, gloss, json.dumps(raw_json)),
        )
        conn.commit()


def render(tree) -> str:
    from rich.console import Console

    console = Console(record=True, width=200)
    console.print(tree)
    return console.export_text()


def test_extract_raw_material_recovers_the_quoted_sentence():
    gloss = "Distilled from raw polemical sentence: The cult of political elites is cultural suicide."
    assert extract_raw_material(gloss) == "The cult of political elites is cultural suicide."


def test_extract_raw_material_returns_none_without_the_marker():
    assert extract_raw_material("just a plain gloss") is None


def test_build_lineage_tree_raises_for_unknown_axiom(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with pytest.raises(LineageNotFoundError):
        build_lineage_tree(cfg, "does_not_exist")


def test_build_lineage_tree_leaf_node_has_no_provenance(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    insert_entry(
        cfg, "n1", "tawhid", "Unity admits no rival.",
        "Distilled from raw polemical sentence: the machine wants to be god.",
        {},
    )

    tree = build_lineage_tree(cfg, "n1")
    text = render(tree)

    assert "n1" in text
    assert "Unity admits no rival." in text
    assert "the machine wants to be god." in text
    assert "no provenance" in text


def test_build_lineage_tree_walks_a_single_stratum_down_to_raw_material(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    insert_entry(
        cfg, "n1", "tawhid", "Unity admits no rival.",
        "Distilled from raw polemical sentence: the machine wants to be god.",
        {},
    )
    insert_entry(
        cfg, "n2", "multiplicity", "Multiplicity fractures the real.",
        "Distilled from raw polemical sentence: everything is fragments now.",
        {},
    )
    insert_entry(
        cfg, "gen1", "recursive_axiom", "The fragment worships the machine that unifies it.",
        "Generated from ontological pressure: unity versus fragmentation. Critic: genuine synthesis.",
        {
            "provenance": {
                "stratum": 1,
                "source_edges": [
                    {
                        "source_id": "n1", "target_id": "n2",
                        "source_category": "tawhid", "target_category": "multiplicity",
                        "pressure_score": 1.2, "reason": "unity versus fragmentation",
                    }
                ],
            }
        },
    )

    tree = build_lineage_tree(cfg, "gen1")
    text = render(tree)

    assert "gen1" in text
    assert "stratum 0001" in text
    assert "unity versus fragmentation" in text
    assert "n1" in text and "Unity admits no rival." in text
    assert "n2" in text and "Multiplicity fractures the real." in text
    assert "the machine wants to be god." in text
    assert "everything is fragments now." in text


def test_build_lineage_tree_recurses_through_multiple_strata(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    insert_entry(
        cfg, "n1", "tawhid", "Unity admits no rival.",
        "Distilled from raw polemical sentence: the machine wants to be god.",
        {},
    )
    insert_entry(
        cfg, "n2", "multiplicity", "Multiplicity fractures the real.", "", {},
    )
    insert_entry(
        cfg, "gen1_0001", "recursive_axiom", "Stratum-1 axiom.",
        "Generated from ontological pressure: unity versus fragmentation.",
        {
            "provenance": {
                "stratum": 1,
                "source_edges": [{
                    "source_id": "n1", "target_id": "n2",
                    "source_category": "tawhid", "target_category": "multiplicity",
                    "pressure_score": 1.2, "reason": "unity versus fragmentation",
                }],
            }
        },
    )
    insert_entry(
        cfg, "n3", "logos", "The word precedes the record.", "", {},
    )
    insert_entry(
        cfg, "gen2_0001", "recursive_axiom", "Stratum-2 axiom, built from a stratum-1 axiom.",
        "Generated from ontological pressure: meaning versus administration.",
        {
            "provenance": {
                "stratum": 2,
                "source_edges": [{
                    "source_id": "gen1_0001", "target_id": "n3",
                    "source_category": "recursive_axiom", "target_category": "logos",
                    "pressure_score": 1.1, "reason": "meaning versus administration",
                }],
            }
        },
    )

    tree = build_lineage_tree(cfg, "gen2_0001")
    text = render(tree)

    assert "stratum 0002" in text
    assert "gen1_0001" in text
    # Recursion into gen1_0001's own provenance reaches stratum 1 and the
    # original raw-material sentence, several levels below the root.
    assert "stratum 0001" in text
    assert "the machine wants to be god." in text


def insert_cycle(
    cfg: AppConfig,
    query: str,
    verdict: str,
    final_score: float,
    parent_cycle_ids: list[int] | None = None,
) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            """
            INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, parent_cycle_ids)
            VALUES (?, 'test-model', 'prompt', 'response', 0, ?, ?, 'a fragment', ?)
            """,
            (query, verdict, final_score, json.dumps(parent_cycle_ids) if parent_cycle_ids else None),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)


def test_build_lineage_tree_dispatches_to_cycle_genealogy_for_a_numeric_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    grandparent = insert_cycle(cfg, "grandparent query", "CANON", 0.9)
    parent = insert_cycle(cfg, "parent query", "CANON", 0.85, parent_cycle_ids=[grandparent])
    child = insert_cycle(cfg, "child query", "CANON", 0.95, parent_cycle_ids=[parent])

    tree = build_lineage_tree(cfg, str(child))
    text = render(tree)

    assert f"cycle `{child}`" in text
    assert f"cycle `{parent}`" in text
    assert f"cycle `{grandparent}`" in text
    assert "child query" in text


def test_build_lineage_tree_marks_retired_ancestors(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    parent = insert_cycle(cfg, "a retired parent", "CANON", 0.9)
    with connect(cfg.database) as conn:
        conn.execute(
            "UPDATE cycles SET retired_at = CURRENT_TIMESTAMP, retirement_reason = ? WHERE id = ?",
            ("no longer clears canon", parent),
        )
        conn.commit()

    child = insert_cycle(cfg, "a child of the retired one", "CANON", 0.95, parent_cycle_ids=[parent])

    tree = build_lineage_tree(cfg, str(child))
    text = render(tree)

    assert "RETIRED" in text
    assert "no longer clears canon" in text


def test_build_lineage_tree_reports_no_parents_for_a_root_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "a root cycle", "CANON", 0.9)

    tree = build_lineage_tree(cfg, str(cycle_id))
    text = render(tree)

    assert "no parent canon fragments recorded" in text


def test_build_lineage_tree_raises_for_unknown_cycle_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with pytest.raises(LineageNotFoundError):
        build_lineage_tree(cfg, "999999")


def test_axiom_lineage_resolves_raw_material_to_an_exact_chunk_location(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO sources(title, path, source_type) VALUES ('Sample Book', '/dev/null', 'book')"
        )
        source_id = conn.execute("SELECT id FROM sources WHERE title = 'Sample Book'").fetchone()["id"]
        content = "some preamble text the machine wants to be god. and some trailing text"
        conn.execute(
            """
            INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate, char_start, char_end)
            VALUES (?, 0, 'Sample Book / chunk 00000', ?, 10, 0, ?)
            """,
            (source_id, content, len(content)),
        )
        conn.commit()

    insert_entry(
        cfg, "n1", "tawhid", "Unity admits no rival.",
        "Distilled from raw polemical sentence: the machine wants to be god.",
        {},
    )

    tree = build_lineage_tree(cfg, "n1")
    text = render(tree)

    assert "Sample Book" in text
    assert "chunk" in text


def test_axiom_lineage_branches_into_canon_genealogy_via_source_cycle_ids(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    ancestor_cycle = insert_cycle(cfg, "an ancestral canon cycle", "CANON", 0.9)

    insert_entry(
        cfg, "n1", "tawhid", "Unity admits no rival.", "", {},
    )
    insert_entry(
        cfg, "n2", "multiplicity", "Multiplicity fractures the real.", "", {},
    )
    insert_entry(
        cfg, "gen1", "recursive_axiom", "A generated axiom tracing to a canon cycle.",
        "Generated from ontological pressure: unity versus fragmentation.",
        {
            "provenance": {
                "stratum": 1,
                "source_edges": [{
                    "source_id": "n1", "target_id": "n2",
                    "source_category": "tawhid", "target_category": "multiplicity",
                    "pressure_score": 1.2, "reason": "unity versus fragmentation",
                }],
                "source_cycle_ids": [ancestor_cycle],
            }
        },
    )

    tree = build_lineage_tree(cfg, "gen1")
    text = render(tree)

    assert "canon genealogy" in text
    assert f"cycle `{ancestor_cycle}`" in text
    assert "an ancestral canon cycle" not in text  # query text isn't printed in this shallow branch, id/verdict/score are


def test_build_lineage_tree_reports_missing_source_axioms_without_crashing(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    insert_entry(
        cfg, "gen1", "recursive_axiom", "An axiom whose sources were never ingested.",
        "Generated from ontological pressure: some reason.",
        {
            "provenance": {
                "stratum": 1,
                "source_edges": [{
                    "source_id": "missing_1", "target_id": "missing_2",
                    "source_category": "a", "target_category": "b",
                    "pressure_score": 1.0, "reason": "some reason",
                }],
            }
        },
    )

    tree = build_lineage_tree(cfg, "gen1")
    text = render(tree)

    assert "missing_1" in text
    assert "not found" in text
