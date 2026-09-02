from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.cycle import run_cycle
from fieldhorizon.db import init_db
from fieldhorizon.evaluate import CycleEvaluation
from fieldhorizon.events import EventRepository
from fieldhorizon.multicycle import run_multi_cycle


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


@pytest.fixture(autouse=True)
def _no_manifest_or_weather_recording():
    # record_operation_manifest (Implementation Brief III, Phase B) calls
    # get_or_register_model, a real /api/tags network call on an
    # unrecognized model. record_cycle_weather (the earlier weather-
    # observability phase) embeds the fragment AND, on first use, all 8
    # weather axes' anchor statements (~74 real embedding calls) -- the
    # actual cause of this file's tests running at ~0.8s instead of
    # near-instant. Both are out of scope for these event-emission tests
    # (manifest recording is covered in test_manifests_wiring.py).
    with (
        patch("fieldhorizon.cycle.record_operation_manifest"),
        patch("fieldhorizon.multicycle.record_operation_manifest"),
        patch("fieldhorizon.cycle.record_cycle_weather"),
        patch("fieldhorizon.multicycle.record_cycle_weather"),
    ):
        yield


def test_run_cycle_emits_the_full_chain_for_a_non_canon_verdict(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.cycle.generate_fragment", return_value="a generated fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", return_value=_evaluation("HERESY")),
    ):
        cycle_id = run_cycle(cfg, "a query", dry_run=False)

    events = EventRepository(cfg).recent(limit=20)
    events.reverse()  # recent() is newest-first; want chronological
    event_types = [e.event_type for e in events]

    assert event_types == ["CycleStarted", "RetrievalCompleted", "EvaluationCompleted", "CycleCompleted"]
    assert len({e.correlation_id for e in events}) == 1  # all one operation
    assert events[-1].aggregate_id == str(cycle_id)
    assert events[0].aggregate_id is None  # no cycle row exists yet at CycleStarted

    # Causation chain: each event's causation_id is the previous event's event_id.
    for prev, curr in zip(events, events[1:], strict=False):
        assert curr.causation_id == prev.event_id


def test_run_cycle_emits_fragment_promoted_for_a_canon_verdict(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.cycle.generate_fragment", return_value="a generated fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", return_value=_evaluation("CANON")),
        patch("fieldhorizon.cycle.embed_and_store_fragment"),
        patch("fieldhorizon.cycle.record_canon_ngrams"),
    ):
        run_cycle(cfg, "a query", dry_run=False)

    events = EventRepository(cfg).recent(limit=20)
    events.reverse()
    event_types = [e.event_type for e in events]

    assert event_types == [
        "CycleStarted", "RetrievalCompleted", "EvaluationCompleted", "FragmentPromoted", "CycleCompleted",
    ]

    # The FragmentPromoted event was cross-emitted by record_canon_event,
    # and points back at the exact canon_events row it corresponds to.
    from fieldhorizon.db import connect

    promoted = next(e for e in events if e.event_type == "FragmentPromoted")
    assert promoted.canon_event_id is not None
    with connect(cfg.database) as conn:
        canon_row = conn.execute("SELECT event FROM canon_events WHERE id = ?", (promoted.canon_event_id,)).fetchone()
    assert canon_row["event"] == "PROMOTED"

    # And it joins the SAME correlation chain as every other event in this
    # cycle -- not a fresh, disconnected one.
    assert promoted.correlation_id == events[0].correlation_id
    # CycleCompleted's causation chains from FragmentPromoted, not silently
    # from EvaluationCompleted (which would happen if adopt() weren't called).
    completed = events[-1]
    assert completed.causation_id == promoted.event_id


def test_run_cycle_emits_cycle_failed_and_still_raises(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.cycle.generate_fragment", side_effect=RuntimeError("model unreachable")),
        pytest.raises(RuntimeError, match="model unreachable"),
    ):
        run_cycle(cfg, "a query", dry_run=False)

    events = EventRepository(cfg).recent(limit=20)
    events.reverse()
    event_types = [e.event_type for e in events]

    assert event_types == ["CycleStarted", "RetrievalCompleted", "CycleFailed"]
    assert events[-1].payload["error_type"] == "RuntimeError"
    assert "model unreachable" in events[-1].payload["error_message"]


def test_run_cycle_defaults_actor_to_cli_and_honors_an_explicit_actor(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.cycle.generate_fragment", return_value="fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", return_value=_evaluation("HERESY")),
    ):
        run_cycle(cfg, "q1", dry_run=False)
        run_cycle(cfg, "q2", dry_run=False, actor="server")

    events = EventRepository(cfg).recent(limit=20)
    actors = {e.actor for e in events}
    assert actors == {"cli", "server"}


def test_run_cycle_dry_run_still_emits_the_chain_but_no_fragment_promoted(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with patch("fieldhorizon.cycle.evaluate_cycle", return_value=_evaluation("CANON")):
        run_cycle(cfg, "a query", dry_run=True)

    events = EventRepository(cfg).recent(limit=20)
    events.reverse()
    event_types = [e.event_type for e in events]

    # dry_run skips the CANON-promotion branch entirely (existing behavior,
    # unchanged) -- so FragmentPromoted must not fire even though verdict is CANON.
    assert "FragmentPromoted" not in event_types
    assert event_types == ["CycleStarted", "RetrievalCompleted", "EvaluationCompleted", "CycleCompleted"]


def test_run_multi_cycle_emits_agent_completed_per_agent(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    from fieldhorizon.agents import AGENTS, AgentOutput

    # Mock the per-agent call (not run_agents itself), so run_agents' real
    # loop -- which invokes on_agent_done after each agent -- actually
    # executes and exercises our AgentCompleted-emitting wrapper.
    def fake_run_agent(cfg, agent, base_prompt, model=None):
        return AgentOutput(name=agent["name"], role=agent["role"], content="a memorandum")

    with (
        patch("fieldhorizon.multicycle.interpret_query", return_value={}),
        patch("fieldhorizon.multicycle.interpretation_to_prompt", return_value=""),
        patch("fieldhorizon.multicycle.export_rust_pressure_report_all", side_effect=RuntimeError("no rust")),
        patch("fieldhorizon.agents.run_agent", side_effect=fake_run_agent),
        patch("fieldhorizon.multicycle.generate_fragment", return_value="a synthesized fragment"),
        patch("fieldhorizon.multicycle.evaluate_cycle", return_value=_evaluation("HERESY")),
    ):
        run_multi_cycle(cfg, "a query", dry_run=False, auto_rewrite=False)

    events = EventRepository(cfg).recent(limit=20)
    events.reverse()
    event_types = [e.event_type for e in events]

    assert event_types.count("AgentCompleted") == len(AGENTS)
    assert event_types[0] == "CycleStarted"
    assert event_types[-1] == "CycleCompleted"


def test_facade_cycle_produces_the_same_event_shape_for_cli_and_server_actors(tmp_path):
    """CLI/server parity: FieldHorizonEngine.cycle() is the one call path both use."""
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    from fieldhorizon.engine import FieldHorizonEngine

    with (
        patch("fieldhorizon.cycle.generate_fragment", return_value="fragment"),
        patch("fieldhorizon.cycle.evaluate_cycle", return_value=_evaluation("HERESY")),
    ):
        FieldHorizonEngine(cfg).cycle("q1", dry_run=False)  # default actor: cli
        FieldHorizonEngine(cfg).cycle("q2", dry_run=False, actor="server")

    repo = EventRepository(cfg)
    all_events = repo.recent(limit=20)
    cli_types = sorted({e.event_type for e in all_events if e.actor == "cli"})
    server_types = sorted({e.event_type for e in all_events if e.actor == "server"})
    assert cli_types == server_types
