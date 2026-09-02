from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import init_db
from fieldhorizon.dream import DreamRegion, run_dream
from fieldhorizon.events import EventRepository


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


_FAKE_REGION = DreamRegion(kind="domain", label="tawhid", query="tawhid", detail={"domain": "tawhid"})


def test_run_dream_emits_started_region_selected_and_completed(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.dream.choose_region", return_value=_FAKE_REGION),
        patch("fieldhorizon.dream.run_region_iteration", return_value={"action": "cycle", "cycle_id": 1, "verdict": "HERESY"}),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run_dream(cfg, budget_cycles=1, budget_minutes=60.0)

    events = EventRepository(cfg).recent(limit=20)
    events.reverse()
    event_types = [e.event_type for e in events]

    assert event_types == ["DreamStarted", "RegionSelected", "DreamCompleted"]
    assert all(e.actor == "dream" for e in events)
    assert len({e.correlation_id for e in events}) == 1


def test_run_dream_emits_region_selected_once_per_iteration(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.dream.choose_region", return_value=_FAKE_REGION),
        patch("fieldhorizon.dream.run_region_iteration", return_value={"action": "cycle", "cycle_id": 1, "verdict": "HERESY"}),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run_dream(cfg, budget_cycles=3, budget_minutes=60.0)

    events = EventRepository(cfg).recent(limit=20)
    event_types = [e.event_type for e in events]
    assert event_types.count("RegionSelected") == 3


def test_run_dream_emits_proposal_emitted_per_proposal(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    fake_evidence = [{"motif": "the veiled machine", "count": 6, "sample_chunks": []}]

    with (
        patch("fieldhorizon.dream.choose_region", return_value=None),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=fake_evidence),
    ):
        run_dream(cfg, budget_cycles=5, budget_minutes=60.0)

    events = EventRepository(cfg).recent(limit=20)
    events.reverse()
    event_types = [e.event_type for e in events]

    assert "DreamProposalEmitted" in event_types
    proposal_event = next(e for e in events if e.event_type == "DreamProposalEmitted")
    assert proposal_event.payload["motif"] == "the veiled machine"


def test_run_dream_emits_dream_failed_and_still_raises(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.dream.choose_region", return_value=_FAKE_REGION),
        patch("fieldhorizon.dream.run_region_iteration", side_effect=RuntimeError("region blew up")),
        pytest.raises(RuntimeError, match="region blew up"),
    ):
        run_dream(cfg, budget_cycles=1, budget_minutes=60.0)

    events = EventRepository(cfg).recent(limit=20)
    events.reverse()
    event_types = [e.event_type for e in events]

    assert event_types == ["DreamStarted", "RegionSelected", "DreamFailed"]
    assert events[-1].payload["error_type"] == "RuntimeError"


def test_nested_cycle_from_a_dream_iteration_gets_its_own_correlation_but_dream_actor(tmp_path):
    """
    Design choice (Phase A dossier, open question 8): a cycle triggered
    from inside a dream iteration mints its own correlation_id rather than
    inheriting the dream run's -- so `events trace <correlation-id>` stays
    meaningful at cycle granularity -- but is still tagged actor="dream",
    distinguishing it from a CLI- or server-triggered cycle.
    """
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    domain_region = DreamRegion(kind="domain", label="tawhid", query="tawhid", detail={"domain": "tawhid"})

    from fieldhorizon.evaluate import CycleEvaluation

    def fake_evaluation(*args, **kwargs):
        return CycleEvaluation(
            symbolic_density=1.0, symbolic_density_method="keyword",
            doctrinal_enforcement=1.0, doctrinal_enforcement_method="keyword",
            length_score=1.0, structure_score=1.0, stuffing_penalty=0.0, generic_penalty=0.0,
            final_score=0.9, verdict="HERESY", notes=[],
        )

    with (
        patch("fieldhorizon.dream.choose_region", return_value=domain_region),
        patch("fieldhorizon.cycle.generate_fragment", return_value="a fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", side_effect=fake_evaluation),
        # verdict="HERESY" makes should_rewrite() true, which would otherwise
        # trigger a real (slow, network-bound) critique/rewrite round-trip --
        # not what this test is about, so it's mocked out too.
        patch("fieldhorizon.cycle.critique_response", return_value="a critique"),
        patch("fieldhorizon.cycle.rewrite_response", return_value="a rewritten response"),
        patch("fieldhorizon.cycle.record_cycle_weather"),
        patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no embedding model in this test")),
        patch("fieldhorizon.dream.backfill_chunk_embeddings", return_value=0),
        patch("fieldhorizon.dream.backfill_axiom_embeddings", return_value=0),
        patch("fieldhorizon.dream.scan_for_ontology_proposals", return_value=[]),
    ):
        run_dream(cfg, budget_cycles=1, budget_minutes=60.0)

    events = EventRepository(cfg).recent(limit=20)
    dream_level = [e for e in events if e.aggregate_type == "dream_run"]
    cycle_level = [e for e in events if e.aggregate_type == "cycle"]

    assert dream_level  # DreamStarted, RegionSelected, DreamCompleted
    assert cycle_level  # CycleStarted..CycleCompleted, from the nested run_cycle call
    assert all(e.actor == "dream" for e in cycle_level)
    # Different correlation chains -- the nested cycle is its own operation.
    assert {e.correlation_id for e in dream_level}.isdisjoint({e.correlation_id for e in cycle_level})
