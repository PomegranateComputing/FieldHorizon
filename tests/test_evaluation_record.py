"""
Original evaluation persistence vs. later recomputation.

Regression for the Windows qualification (lot 00): the reproducible bundle
showed a "recomputed" 0.919 CANON for a cycle persisted as 0.819
USEFUL_FRAGMENT, because the recomputation scored the text with no retrieved
axioms. These tests pin the separation: original components are written at
decision time; a recomputation runs in the cycle's own recorded context and
says so; a cycle that predates persistence reports its components as
unavailable instead of presenting a recomputation as original.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from fieldhorizon.cli import main
from fieldhorizon.config import AppConfig
from fieldhorizon.cycle import run_cycle
from fieldhorizon.db import connect, init_db
from fieldhorizon.engine import FieldHorizonEngine
from fieldhorizon.evaluate import EVALUATOR_VERSION, CycleEvaluation
from fieldhorizon.evaluation_record import STATUS_RECORDED, STATUS_UNAVAILABLE, cycle_evaluation_report
from fieldhorizon.ingest import ingest_json_corpus
from fieldhorizon.server import create_app

TOKEN = "test-token"

AXIOMS = [
    {"id": "logos_0001", "group_name": "logos", "category": "logos", "tradition": "christian",
     "statement": "The Word precedes every system that claims to explain reality.", "gloss": "g"},
    {"id": "tech_0001", "group_name": "technology", "category": "technology", "tradition": "modern",
     "statement": "The machine extends intention but cannot supply meaning.", "gloss": "g"},
]

FRAGMENT = (
    "The machine is a system that claims to explain reality, yet the Word precedes it.\n\n"
    "Meaning is not supplied by the machine; intention is merely extended into metal."
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


def _fake_enforcement(cfg, response, json_rows):
    # Mirrors the real failure mode deterministically: with no axioms the
    # score is vacuously perfect; with axioms it depends on the context.
    return (1.0, "keyword") if not json_rows else (0.5, "keyword")


@pytest.fixture(autouse=True)
def _offline():
    with (
        patch("fieldhorizon.evaluate.score_doctrinal_enforcement", side_effect=_fake_enforcement),
        patch("fieldhorizon.evaluate.score_symbolic_density", return_value=(0.5, "keyword")),
        patch("fieldhorizon.cycle.record_operation_manifest"),
        patch("fieldhorizon.cycle.record_cycle_weather"),
        patch("fieldhorizon.cycle.generate_fragment", return_value=FRAGMENT),
    ):
        yield


@pytest.fixture
def cfg(tmp_path):
    cfg = make_config(tmp_path)
    cfg.json_corpus.mkdir(parents=True)
    (cfg.json_corpus / "axioms.json").write_text(json.dumps(AXIOMS), encoding="utf-8")
    init_db(cfg.database)
    ingest_json_corpus(cfg)
    return cfg


def _rows(cfg, cycle_id):
    with connect(cfg.database) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM cycle_evaluations WHERE cycle_id = ?", (cycle_id,))]


def test_a_cycle_persists_its_original_components_with_the_evaluator_version(cfg):
    cycle_id = run_cycle(cfg, "machine meaning reality", dry_run=False)

    rows = _rows(cfg, cycle_id)
    assert [(r["role"], r["selected"]) for r in rows] == [("initial", 1)]
    row = rows[0]
    assert row["evaluator_version"] == EVALUATOR_VERSION
    components = json.loads(row["components_json"])
    context = json.loads(row["context_json"])
    for key in ("doctrinal_enforcement", "stuffing_penalty", "canon_loop_penalty", "truncation_penalty",
                "word_count", "paragraph_count", "canon_gate_passed"):
        assert key in components
    assert context["json_refs"], "the cycle retrieved axioms, so its context must name them"
    assert context["parameters"]["weights"]["doctrinal_enforcement"] > 0

    with connect(cfg.database) as conn:
        cycle = conn.execute("SELECT final_score, verdict FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        json_refs = [r["ref"] for r in conn.execute(
            "SELECT ref FROM cycle_sources WHERE cycle_id = ? AND source_kind = 'json'", (cycle_id,))]
    assert row["final_score"] == cycle["final_score"]
    assert row["verdict"] == cycle["verdict"]
    assert context["json_refs"] == json_refs


def test_recomputation_runs_in_the_original_context_and_matches(cfg):
    cycle_id = run_cycle(cfg, "machine meaning reality", dry_run=False)

    report = cycle_evaluation_report(cfg, cycle_id)

    assert report.original_status == STATUS_RECORDED
    recomputation = report.recomputation
    assert recomputation.matches_persisted_decision
    assert recomputation.json_context_matches_original is True
    assert recomputation.same_evaluator_version is True
    assert recomputation.component_deltas == {}


def test_the_old_contextless_preview_differs_but_the_recomputation_does_not(cfg):
    """The exact qualification defect: re-scoring without axioms is a different evaluation."""
    cycle_id = run_cycle(cfg, "machine meaning reality", dry_run=False)
    report = cycle_evaluation_report(cfg, cycle_id)

    with connect(cfg.database) as conn:
        fragment = conn.execute("SELECT fragment FROM cycles WHERE id = ?", (cycle_id,)).fetchone()["fragment"]
    contextless = FieldHorizonEngine(cfg).evaluate_fragment(fragment)

    assert contextless.doctrinal_enforcement != report.original[0].components["doctrinal_enforcement"]
    assert contextless.final_score != report.persisted_final_score
    assert report.recomputation.final_score == report.persisted_final_score


def test_a_cycle_that_predates_persistence_reports_unavailable_never_reconstructed(cfg):
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q', 'm', 'p', ?, 0, 'USEFUL_FRAGMENT', 0.819, ?)",
            (FRAGMENT, FRAGMENT),
        )
        conn.execute(
            "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (?, 'json', 'logos_0001', 's')",
            (cur.lastrowid,),
        )
        conn.commit()
        cycle_id = int(cur.lastrowid)

    report = cycle_evaluation_report(cfg, cycle_id)

    assert report.original_status == STATUS_UNAVAILABLE
    assert "predates" in report.original_unavailable_reason
    assert report.original == []
    assert report.persisted_final_score == 0.819
    assert report.persisted_verdict == "USEFUL_FRAGMENT"
    assert report.recomputation is not None
    assert report.recomputation.component_deltas is None
    assert report.recomputation.json_context_matches_original is None


def test_a_changed_or_missing_context_is_reported(cfg):
    cycle_id = run_cycle(cfg, "machine meaning reality", dry_run=False)
    refs = cycle_evaluation_report(cfg, cycle_id, recompute=False).original[0].context["json_refs"]

    with connect(cfg.database) as conn:
        conn.execute("UPDATE json_entries SET statement = 'edited' WHERE id = ?", (refs[0],))
        conn.commit()
    assert cycle_evaluation_report(cfg, cycle_id).recomputation.json_context_matches_original is False

    with connect(cfg.database) as conn:
        conn.execute("DELETE FROM json_entries WHERE id = ?", (refs[0],))
        conn.commit()
    assert refs[0] in cycle_evaluation_report(cfg, cycle_id).recomputation.missing_json_refs


def _evaluation(score: float, verdict: str) -> CycleEvaluation:
    return CycleEvaluation(
        symbolic_density=0.5, symbolic_density_method="keyword",
        doctrinal_enforcement=0.5, doctrinal_enforcement_method="keyword",
        length_score=1.0, structure_score=1.0, stuffing_penalty=0.0, generic_penalty=0.0,
        final_score=score, verdict=verdict, notes=[],
    )


def test_a_rewritten_cycle_records_both_evaluations_and_which_was_selected(cfg):
    with (
        patch("fieldhorizon.cycle.evaluate_cycle", side_effect=[_evaluation(0.55, "HERESY"), _evaluation(0.7, "USEFUL_FRAGMENT")]),
        patch("fieldhorizon.cycle.should_rewrite", return_value=True),
        patch("fieldhorizon.cycle.critique_response", return_value="critique"),
        patch("fieldhorizon.cycle.rewrite_response", return_value="FIELD_FRAGMENT\nrewritten"),
    ):
        cycle_id = run_cycle(cfg, "machine meaning reality", dry_run=False, auto_rewrite=True)

    rows = {r["role"]: r for r in _rows(cfg, cycle_id)}
    assert set(rows) == {"initial", "rewritten"}
    assert rows["initial"]["selected"] == 0 and rows["initial"]["final_score"] == 0.55
    assert rows["rewritten"]["selected"] == 1 and rows["rewritten"]["final_score"] == 0.7


def test_the_api_separates_persisted_original_and_recomputed(cfg):
    cycle_id = run_cycle(cfg, "machine meaning reality", dry_run=False)
    client = TestClient(create_app(cfg, TOKEN))
    headers = {"Authorization": f"Bearer {TOKEN}"}

    body = client.get(f"/cycles/{cycle_id}/evaluation", headers=headers).json()
    assert body["original_status"] == "recorded"
    assert body["original"][0]["evaluator_version"] == EVALUATOR_VERSION
    assert body["recomputation"]["matches_persisted_decision"] is True

    persisted_only = client.get(f"/cycles/{cycle_id}/evaluation?recompute=false", headers=headers).json()
    assert persisted_only["recomputation"] is None

    assert client.get("/cycles/9999/evaluation", headers=headers).status_code == 404
    assert client.get(f"/cycles/{cycle_id}/evaluation").status_code in (401, 403)


def test_the_cli_reports_the_same_record(cfg, tmp_path, capsys):
    cycle_id = run_cycle(cfg, "machine meaning reality", dry_run=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "paths:\n"
        f'  database: "{cfg.database.as_posix()}"\n'
        f'  json_corpus: "{cfg.json_corpus.as_posix()}"\n'
        f'  outputs: "{cfg.outputs.as_posix()}"\n'
        f'  logs: "{cfg.logs.as_posix()}"\n',
        encoding="utf-8",
    )
    with patch("sys.argv", ["field-horizon", "--config", str(config_path), "cycle-evaluation", str(cycle_id), "--json"]):
        main()
    body = json.loads(capsys.readouterr().out)
    assert body["cycle_id"] == cycle_id
    assert body["original_status"] == "recorded"
    assert body["recomputation"]["matches_persisted_decision"] is True
