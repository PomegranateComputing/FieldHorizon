from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.cycle import run_cycle
from fieldhorizon.db import CURRENT_SCHEMA_VERSION, init_db
from fieldhorizon.evaluate import CycleEvaluation
from fieldhorizon.manifests import RunManifestRepository
from fieldhorizon.registries import _model_id_cache


@pytest.fixture(autouse=True)
def _clear_model_id_cache():
    # _model_id_cache is process-global (see test_registries.py) -- clear
    # it so this test's forced-failure /api/tags mock actually takes
    # effect, regardless of what an earlier test in the same session
    # already cached for "hermes3:8b".
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


def test_run_cycle_records_a_manifest_keyed_by_its_own_correlation_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.cycle.generate_fragment", return_value="a fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", return_value=_evaluation("HERESY")),
        patch("fieldhorizon.cycle.record_cycle_weather"),
        patch("fieldhorizon.registries.requests.get", side_effect=RuntimeError("no ollama")),
    ):
        cycle_id = run_cycle(cfg, "a query", dry_run=False)

    from fieldhorizon.events import EventRepository

    events = EventRepository(cfg).recent(limit=20)
    correlation_id = events[0].correlation_id  # all events from this cycle share one correlation_id

    manifest = RunManifestRepository(cfg).get(correlation_id)
    assert manifest is not None
    assert manifest.operation == "cycle"
    assert manifest.output_ids == [cycle_id]
    assert manifest.model_registry_ids == ["hermes3:8b"]
    assert manifest.ontology_fingerprint is not None
    assert manifest.db_schema_version == CURRENT_SCHEMA_VERSION
    assert manifest.selected_evidence_ids == []  # no book/json/canon rows exist in this empty test DB


def test_run_cycle_manifest_failure_does_not_fail_the_cycle(tmp_path):
    """Manifest recording is best-effort -- a failure inside it must not turn a successful cycle into an exception."""
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.cycle.generate_fragment", return_value="a fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", return_value=_evaluation("HERESY")),
        patch("fieldhorizon.cycle.record_cycle_weather"),
        patch("fieldhorizon.cycle.record_operation_manifest", side_effect=RuntimeError("disk full")),
    ):
        cycle_id = run_cycle(cfg, "a query", dry_run=False)  # must not raise

    assert cycle_id is not None


def test_run_council_records_a_manifest(tmp_path):
    from fieldhorizon.council import run_council
    from fieldhorizon.events import EventRepository

    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with patch("fieldhorizon.council.record_council_weather"):
        report = run_council(cfg, sample=5, min_age_days=7)

    events = EventRepository(cfg).recent(limit=20)
    correlation_id = events[0].correlation_id

    manifest = RunManifestRepository(cfg).get(correlation_id)
    assert manifest is not None
    assert manifest.operation == "council"
    assert manifest.output_ids == [report.council_id]


def test_run_dream_records_a_manifest_with_seed_and_output_ids(tmp_path):
    from fieldhorizon.dream import DreamRegion, run_dream

    cfg = make_config(tmp_path)
    init_db(cfg.database)

    fake_region = DreamRegion(kind="domain", label="tawhid", query="tawhid", detail={"domain": "tawhid"})

    with (
        patch("fieldhorizon.dream.choose_region", return_value=fake_region),
        patch("fieldhorizon.dream.run_region_iteration", return_value={"action": "cycle", "cycle_id": 42, "verdict": "HERESY"}),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run = run_dream(cfg, budget_cycles=1, budget_minutes=60.0, seed=1234)

    from fieldhorizon.events import EventRepository

    events = EventRepository(cfg).recent(limit=20)
    correlation_id = next(e.correlation_id for e in events if e.aggregate_type == "dream_run")

    manifest = RunManifestRepository(cfg).get(correlation_id)
    assert manifest is not None
    assert manifest.operation == "dream"
    assert manifest.random_seeds == {"seed": 1234}
    assert manifest.output_ids == ["cycle:42"]
    assert run.iterations == 1
