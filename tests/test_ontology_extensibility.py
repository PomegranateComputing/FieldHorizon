from __future__ import annotations

from fieldhorizon import ontology_spec
from fieldhorizon.doctrinal import score_doctrinal_enforcement_keyword
from fieldhorizon.ontology import OntologyNode, build_pressure_edges
from fieldhorizon.retrieval import detect_domains, wanted_sources

# A brand-new domain plus a brand-new opposition, added the only way review
# §4 intends: a YAML block and (implicitly) a JSON corpus file, zero
# changes to retrieval.py / evaluate.py / ontology.py / rewrite.py.
NEW_DOMAIN_ONTOLOGY = """
domains:
  solarpunk:
    hints: [solarpunk, greenhouse, vertical_farm, symbiosis]
    expansions: [garden, canopy, renewal]
    sources: [manifesto, red_flags]
    keywords: [solarpunk, symbiosis, canopy, renewal]
    rewrite_directive: "explicitly develop ecological symbiosis, renewal, and post-scarcity abundance"

oppositions:
  - [solarpunk, bureaucracy, "organic growth versus administrative control"]

source_expansions: {}
"""


def _use_temp_ontology(tmp_path, monkeypatch) -> None:
    ontology_path = tmp_path / "ontology.yaml"
    ontology_path.write_text(NEW_DOMAIN_ONTOLOGY, encoding="utf-8")
    monkeypatch.setattr(ontology_spec, "DEFAULT_ONTOLOGY_PATH", ontology_path)


def test_retrieval_routes_to_a_brand_new_domain_with_zero_code_changes(tmp_path, monkeypatch):
    _use_temp_ontology(tmp_path, monkeypatch)

    domains = detect_domains("a vision of the greenhouse and the vertical_farm")
    assert domains == ["solarpunk"]

    sources = wanted_sources("a vision of the greenhouse and the vertical_farm")
    assert sources == ["manifesto", "red_flags"]


def test_evaluator_scores_the_new_domains_keywords_with_zero_code_changes(tmp_path, monkeypatch):
    _use_temp_ontology(tmp_path, monkeypatch)

    response = (
        "FIELD_FRAGMENT\n"
        "The canopy renews itself in endless symbiosis, a garden that forgives.\n\n"
        "METADATA_JSON\n{}"
    )
    json_rows = [{"category": "solarpunk", "statement": "The garden forgives the machine."}]

    enforcement = score_doctrinal_enforcement_keyword(response, json_rows)
    assert enforcement == 1.0


def test_evaluator_does_not_credit_undeveloped_new_domain_keywords(tmp_path, monkeypatch):
    _use_temp_ontology(tmp_path, monkeypatch)

    response = "FIELD_FRAGMENT\nA fragment that never mentions the new domain at all.\n\nMETADATA_JSON\n{}"
    json_rows = [{"category": "solarpunk", "statement": "The garden forgives the machine."}]

    enforcement = score_doctrinal_enforcement_keyword(response, json_rows)
    assert enforcement == 0.0


def test_pressure_engine_picks_up_a_new_opposition_with_zero_code_changes(tmp_path, monkeypatch):
    _use_temp_ontology(tmp_path, monkeypatch)

    nodes = [
        OntologyNode(id="a", category="solarpunk", tradition="t", statement="s", severity=0.6, mutation_potential=0.4),
        OntologyNode(id="b", category="bureaucracy", tradition="t", statement="s", severity=0.5, mutation_potential=0.3),
        OntologyNode(id="c", category="unrelated", tradition="t", statement="s", severity=0.2, mutation_potential=0.1),
    ]

    edges = build_pressure_edges(nodes)

    assert len(edges) == 1
    assert edges[0].reason == "organic growth versus administrative control"
    assert {edges[0].source_category, edges[0].target_category} == {"solarpunk", "bureaucracy"}
