from __future__ import annotations

import pytest

from fieldhorizon.ontology_spec import OntologySpecError, get_ontology, load_ontology

MINIMAL_ONTOLOGY = """
domains:
  tawhid:
    hints: [tawhid, unity, idol]
    expansions: [worship, lord]
    sources: [sacred_quran, sacred_bible]
    keywords: [tawhid, unity, idol]
    rewrite_directive: "develop unity, idolatry, worship, false mediation, transcendence"
  bare_domain:
    hints: []
    expansions: []
    sources: []
    keywords: []
    rewrite_directive: null

oppositions:
  - [tawhid, multiplicity, "unity versus fragmentation"]

source_expansions:
  sacred_quran: [allah, lord]
"""


def test_real_ontology_yaml_loads_and_validates():
    ontology = get_ontology()
    assert "tawhid" in ontology.domains
    assert "godel_engine" in ontology.domains
    assert len(ontology.oppositions) == 10


def test_domain_accessors(tmp_path):
    cfg_path = tmp_path / "ontology.yaml"
    cfg_path.write_text(MINIMAL_ONTOLOGY, encoding="utf-8")
    ontology = load_ontology(cfg_path)

    assert ontology.hints_for("tawhid") == frozenset({"tawhid", "unity", "idol"})
    assert ontology.keywords_for("tawhid") == ("tawhid", "unity", "idol")
    assert ontology.keywords_for("nonexistent") == ()
    assert ontology.source_expansions_for("sacred_quran") == ("allah", "lord")
    assert ontology.source_expansions_for("nonexistent") == ()


def test_rewrite_directive_direct_and_via_hints(tmp_path):
    cfg_path = tmp_path / "ontology.yaml"
    cfg_path.write_text(MINIMAL_ONTOLOGY, encoding="utf-8")
    ontology = load_ontology(cfg_path)

    assert ontology.rewrite_directive_for("tawhid") is not None
    # "unity" is a hint of the tawhid domain, not its own domain key.
    assert ontology.rewrite_directive_for("unity") == ontology.rewrite_directive_for("tawhid")
    assert ontology.rewrite_directive_for("bare_domain") is None
    assert ontology.rewrite_directive_for("nonexistent") is None


def test_opposition_reason_checks_both_orderings(tmp_path):
    cfg_path = tmp_path / "ontology.yaml"
    cfg_path.write_text(MINIMAL_ONTOLOGY, encoding="utf-8")
    ontology = load_ontology(cfg_path)

    assert ontology.opposition_reason("tawhid", "multiplicity") == "unity versus fragmentation"
    assert ontology.opposition_reason("multiplicity", "tawhid") == "unity versus fragmentation"
    assert ontology.opposition_reason("tawhid", "nonexistent") is None


def test_missing_file_raises_clear_error(tmp_path):
    with pytest.raises(OntologySpecError, match="Missing ontology file"):
        load_ontology(tmp_path / "does_not_exist.yaml")


def test_malformed_domain_raises_clear_error(tmp_path):
    cfg_path = tmp_path / "ontology.yaml"
    cfg_path.write_text(
        """
domains:
  broken: "not a mapping"
oppositions: []
source_expansions: {}
""",
        encoding="utf-8",
    )

    with pytest.raises(OntologySpecError, match="domains.broken must be a mapping"):
        load_ontology(cfg_path)


def test_malformed_opposition_raises_clear_error(tmp_path):
    cfg_path = tmp_path / "ontology.yaml"
    cfg_path.write_text(
        """
domains: {}
oppositions:
  - [only_two_items, "reason"]
source_expansions: {}
""",
        encoding="utf-8",
    )

    with pytest.raises(OntologySpecError, match=r"oppositions\[0\] must be a 3-item list"):
        load_ontology(cfg_path)


def test_non_string_hints_raise_clear_error(tmp_path):
    cfg_path = tmp_path / "ontology.yaml"
    cfg_path.write_text(
        """
domains:
  tawhid:
    hints: [1, 2, 3]
oppositions: []
source_expansions: {}
""",
        encoding="utf-8",
    )

    with pytest.raises(OntologySpecError, match="domains.tawhid.hints must be a list of strings"):
        load_ontology(cfg_path)
