from __future__ import annotations

from fieldhorizon.retrieval import detect_domains, expanded_terms_for_source, wanted_sources


def test_detect_domains_matches_hints_from_ontology_yaml():
    assert "tawhid" in detect_domains("the idol of unity and allah")
    assert "bureaucracy" in detect_domains("a workflow of compliance and procedure")


def test_detect_domains_no_match_returns_empty():
    assert detect_domains("completely unrelated filler text about nothing") == []


def test_wanted_sources_routes_tawhid_to_quran_first():
    sources = wanted_sources("the idol of unity and allah")
    assert sources[0] == "sacred_quran"


def test_wanted_sources_falls_back_when_nothing_detected():
    sources = wanted_sources("completely unrelated filler text about nothing")
    assert sources == ["red_flags", "sacred_quran", "sacred_bible", "manifesto"]


def test_expanded_terms_include_domain_expansions_and_source_expansions():
    expanded = expanded_terms_for_source("sacred_quran", "the idol of unity")
    # "worship"/"lord" come from tawhid's domain expansions or sacred_quran's
    # source expansions in ontology.yaml -- either way, from the ontology.
    assert "worship" in expanded or "lord" in expanded
