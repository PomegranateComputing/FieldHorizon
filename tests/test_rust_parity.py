from __future__ import annotations

import json

import pytest

from fieldhorizon.config import load_config
from fieldhorizon.ontology import OntologyNode, build_pressure_edges
from fieldhorizon.rustcore import run_rust_pressure_core, rust_binary_path

# A fixture spanning several ontology.yaml opposition pairs (tawhid/
# multiplicity, logos/bureaucracy, logos/modernity, apocalypse/modernity)
# plus one non-participating category, so the parity check covers both
# "produces an edge" and "correctly produces no edge".
FIXTURE_ENTRIES: list[dict[str, str | float]] = [
    {"id": "n1", "category": "tawhid", "tradition": "t", "statement": "s1", "severity": 0.8, "mutation_potential": 0.5},
    {"id": "n2", "category": "multiplicity", "tradition": "t", "statement": "s2", "severity": 0.6, "mutation_potential": 0.4},
    {"id": "n3", "category": "logos", "tradition": "t", "statement": "s3", "severity": 0.7, "mutation_potential": 0.3},
    {"id": "n4", "category": "bureaucracy", "tradition": "t", "statement": "s4", "severity": 0.5, "mutation_potential": 0.2},
    {"id": "n5", "category": "modernity", "tradition": "t", "statement": "s5", "severity": 0.9, "mutation_potential": 0.6},
    {"id": "n6", "category": "apocalypse", "tradition": "t", "statement": "s6", "severity": 0.4, "mutation_potential": 0.1},
    {"id": "n7", "category": "unrelated_category", "tradition": "t", "statement": "s7", "severity": 0.3, "mutation_potential": 0.2},
]


def _load_real_config():
    return load_config("config.yaml")


def test_python_and_rust_pressure_engines_agree_on_the_same_input(tmp_path):
    cfg = _load_real_config()
    binary = rust_binary_path(cfg)
    if not binary.exists():
        pytest.skip("fh-core binary not built (cd fieldhorizon-rust && cargo build --release)")

    nodes = [
        OntologyNode(
            id=str(e["id"]),
            category=str(e["category"]),
            tradition=str(e["tradition"]),
            statement=str(e["statement"]),
            severity=float(e["severity"]),
            mutation_potential=float(e["mutation_potential"]),
        )
        for e in FIXTURE_ENTRIES
    ]
    python_edges = build_pressure_edges(nodes)
    python_set = {(edge.source_id, edge.target_id, edge.reason) for edge in python_edges}

    rust_input = [
        {
            "id": e["id"],
            "group_name": None,
            "category": e["category"],
            "tradition": e["tradition"],
            "statement": e["statement"],
            "gloss": None,
            "severity": e["severity"],
            "mutation_potential": e["mutation_potential"],
            "tags": None,
        }
        for e in FIXTURE_ENTRIES
    ]
    input_path = tmp_path / "entries.json"
    input_path.write_text(json.dumps(rust_input), encoding="utf-8")

    rust_report = run_rust_pressure_core(cfg, input_path=input_path, limit=1000)
    rust_set = {(edge["source_id"], edge["target_id"], edge["reason"]) for edge in rust_report["edges"]}

    # Sanity: the fixture is expected to produce edges at all -- an empty
    # set on both sides would make the equality assertion vacuous.
    assert python_set
    assert python_set == rust_set
