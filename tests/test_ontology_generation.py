from __future__ import annotations

import json
from unittest.mock import patch

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.ontology import (
    OntologyNode,
    PressureEdge,
    build_mutation_prompt,
    export_generated_axioms,
    mutate_axiom,
    next_stratum_number,
)


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


SOURCE = OntologyNode(
    id="n1", category="tawhid", tradition="t",
    statement="Unity admits no rival.", gloss="On the oneness of the real.",
    severity=0.8, mutation_potential=0.5,
)
TARGET = OntologyNode(
    id="n2", category="multiplicity", tradition="t",
    statement="Multiplicity fractures the real into administrable parts.",
    gloss="On the proliferation of fragments.",
    severity=0.6, mutation_potential=0.4,
)
EDGE = PressureEdge(
    source_id="n1", target_id="n2",
    source_category="tawhid", target_category="multiplicity",
    pressure_type="ontological_pressure", score=1.2,
    reason="unity versus fragmentation",
)


def test_build_mutation_prompt_includes_actual_statements_and_glosses():
    prompt = build_mutation_prompt(SOURCE, TARGET, EDGE)
    assert SOURCE.statement in prompt
    assert SOURCE.gloss in prompt
    assert TARGET.statement in prompt
    assert TARGET.gloss in prompt
    assert EDGE.reason in prompt


def test_mutate_axiom_parses_llm_json_response(tmp_path):
    cfg = make_config(tmp_path)
    response = json.dumps({
        "statement": "The one who fragments unity still worships a single machine.",
        "severity": 0.9,
        "mutation_potential": 1.5,  # out of range -- must be clamped
    })

    with patch("fieldhorizon.ontology.call_ollama", return_value=response) as mock_call:
        candidate = mutate_axiom(cfg, EDGE, SOURCE, TARGET)

    assert candidate is not None
    assert candidate["statement"] == "The one who fragments unity still worships a single machine."
    assert candidate["severity"] == 0.9
    assert candidate["mutation_potential"] == 1.0  # clamped
    assert candidate["source_edges"][0]["source_id"] == "n1"

    # temperature=0 for this structured-JSON call (review's real-upgrade tier §5).
    assert mock_call.call_args.kwargs["options"] == {"temperature": 0}


def test_mutate_axiom_returns_none_on_llm_failure(tmp_path):
    cfg = make_config(tmp_path)

    with patch("fieldhorizon.ontology.call_ollama", side_effect=RuntimeError("unreachable")):
        assert mutate_axiom(cfg, EDGE, SOURCE, TARGET) is None


def test_mutate_axiom_returns_none_on_missing_statement(tmp_path):
    cfg = make_config(tmp_path)

    with patch("fieldhorizon.ontology.call_ollama", return_value='{"severity": 0.5}'):
        assert mutate_axiom(cfg, EDGE, SOURCE, TARGET) is None


def test_next_stratum_number_starts_at_one(tmp_path):
    cfg = make_config(tmp_path)
    cfg.json_corpus.mkdir(parents=True)
    assert next_stratum_number(cfg) == 1


def test_next_stratum_number_increments_past_existing_strata(tmp_path):
    cfg = make_config(tmp_path)
    cfg.json_corpus.mkdir(parents=True)
    (cfg.json_corpus / "generated_axioms_0001.json").write_text("[]")
    (cfg.json_corpus / "generated_axioms_0002.json").write_text("[]")
    (cfg.json_corpus / "culture_war.json").write_text("[]")  # unrelated file, must be ignored

    assert next_stratum_number(cfg) == 3


def insert_axiom_pair(cfg: AppConfig) -> None:
    with connect(cfg.database) as conn:
        conn.execute(
            """
            INSERT INTO json_entries(id, group_name, category, tradition, statement, gloss, severity, mutation_potential, raw_json)
            VALUES ('n1', 'g', 'tawhid', 't', 'Unity admits no rival.', 'gloss a', 0.8, 0.5, '{}')
            """
        )
        conn.execute(
            """
            INSERT INTO json_entries(id, group_name, category, tradition, statement, gloss, severity, mutation_potential, raw_json)
            VALUES ('n2', 'g', 'multiplicity', 't', 'Multiplicity fractures the real.', 'gloss b', 0.6, 0.4, '{}')
            """
        )
        conn.commit()


def test_export_generated_axioms_writes_only_critic_promoted_candidates(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cfg.json_corpus.mkdir(parents=True)
    insert_axiom_pair(cfg)

    fake_candidate = {
        "statement": "The one who fragments unity still worships a single machine.",
        "severity": 0.9,
        "mutation_potential": 0.8,
        "source_edges": [{
            "source_id": "n1", "target_id": "n2",
            "source_category": "tawhid", "target_category": "multiplicity",
            "pressure_score": 1.2, "reason": "unity versus fragmentation",
        }],
    }

    with (
        patch("fieldhorizon.ontology.mutate_axiom", return_value=fake_candidate),
        patch(
            "fieldhorizon.ontology.critique_axiom_candidate",
            return_value={"verdict": "PROMOTE", "reason": "genuine synthesis"},
        ),
    ):
        out_path = export_generated_axioms(cfg, min_score=0.9)

    assert out_path.name == "generated_axioms_0001.json"
    generated = json.loads(out_path.read_text())
    assert len(generated) == 1
    axiom = generated[0]
    assert axiom["statement"] == fake_candidate["statement"]
    assert axiom["provenance"]["stratum"] == 1
    assert axiom["provenance"]["source_edges"][0]["source_id"] == "n1"


def test_export_generated_axioms_rejects_candidates_the_critic_rejects(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cfg.json_corpus.mkdir(parents=True)
    insert_axiom_pair(cfg)

    fake_candidate = {
        "statement": "A weak restatement of both sides.",
        "severity": 0.5,
        "mutation_potential": 0.5,
        "source_edges": [{
            "source_id": "n1", "target_id": "n2",
            "source_category": "tawhid", "target_category": "multiplicity",
            "pressure_score": 1.2, "reason": "unity versus fragmentation",
        }],
    }

    with (
        patch("fieldhorizon.ontology.mutate_axiom", return_value=fake_candidate),
        patch(
            "fieldhorizon.ontology.critique_axiom_candidate",
            return_value={"verdict": "REJECT", "reason": "mere restatement"},
        ),
    ):
        out_path = export_generated_axioms(cfg, min_score=0.9)

    generated = json.loads(out_path.read_text())
    assert generated == []


def test_export_generated_axioms_versions_strata_instead_of_overwriting(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cfg.json_corpus.mkdir(parents=True)
    insert_axiom_pair(cfg)

    fake_candidate = {
        "statement": "The one who fragments unity still worships a single machine.",
        "severity": 0.9,
        "mutation_potential": 0.8,
        "source_edges": [{
            "source_id": "n1", "target_id": "n2",
            "source_category": "tawhid", "target_category": "multiplicity",
            "pressure_score": 1.2, "reason": "unity versus fragmentation",
        }],
    }

    with (
        patch("fieldhorizon.ontology.mutate_axiom", return_value=fake_candidate),
        patch(
            "fieldhorizon.ontology.critique_axiom_candidate",
            return_value={"verdict": "PROMOTE", "reason": "genuine synthesis"},
        ),
    ):
        first_path = export_generated_axioms(cfg, min_score=0.9)
        second_path = export_generated_axioms(cfg, min_score=0.9)

    assert first_path.name == "generated_axioms_0001.json"
    assert second_path.name == "generated_axioms_0002.json"
    assert first_path.exists()
    assert second_path.exists()
