from __future__ import annotations

from fieldhorizon.ontology import OntologyNode, build_pressure_edges


def node(id_: str, category: str, severity: float = 0.5, mutation_potential: float = 0.5) -> OntologyNode:
    return OntologyNode(
        id=id_,
        category=category,
        tradition="t",
        statement="s",
        severity=severity,
        mutation_potential=mutation_potential,
    )


def test_build_pressure_edges_finds_known_opposition_pairs():
    nodes = [
        node("n1", "tawhid"),
        node("n2", "multiplicity"),
        node("n3", "logos"),
        node("n4", "bureaucracy"),
    ]

    edges = build_pressure_edges(nodes)
    pairs = {(e.source_category, e.target_category, e.reason) for e in edges}

    assert ("tawhid", "multiplicity", "unity versus fragmentation") in pairs or (
        "multiplicity",
        "tawhid",
        "unity versus fragmentation",
    ) in pairs
    assert ("logos", "bureaucracy", "meaning versus administration") in pairs or (
        "bureaucracy",
        "logos",
        "meaning versus administration",
    ) in pairs


def test_build_pressure_edges_ignores_unopposed_categories():
    nodes = [node("n1", "tawhid"), node("n2", "some_unrelated_category")]
    edges = build_pressure_edges(nodes)
    assert edges == []


def test_build_pressure_edges_ignores_blank_category_nodes():
    nodes = [node("n1", ""), node("n2", "")]
    edges = build_pressure_edges(nodes)
    assert edges == []


def test_build_pressure_edges_crosses_all_members_of_opposed_groups():
    # Two nodes per side of a known opposition pair -> all 4 cross edges.
    nodes = [
        node("logos_a", "logos"),
        node("logos_b", "logos"),
        node("bureaucracy_a", "bureaucracy"),
        node("bureaucracy_b", "bureaucracy"),
    ]

    edges = build_pressure_edges(nodes)
    assert len(edges) == 4


def test_build_pressure_edges_sorted_by_score_descending():
    nodes = [
        node("weak_a", "logos", severity=0.1, mutation_potential=0.1),
        node("weak_b", "bureaucracy", severity=0.1, mutation_potential=0.1),
        node("strong_a", "tawhid", severity=0.9, mutation_potential=0.9),
        node("strong_b", "multiplicity", severity=0.9, mutation_potential=0.9),
    ]

    edges = build_pressure_edges(nodes)
    scores = [e.score for e in edges]
    assert scores == sorted(scores, reverse=True)
