from __future__ import annotations

from pathlib import Path

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.events import EventRepository
from fieldhorizon.provenance import (
    OBJECT_COUNCIL,
    OBJECT_CYCLE,
    REL_REHABILITATES,
    REL_RETIRES,
    ProvenanceEdgeRepository,
)
from fieldhorizon.temporal import (
    CREATION_BACKFILL,
    TemporalCanonRepository,
    TemporalNotFoundError,
    backfill_canon_temporal_states,
    canon_as_of_cycle,
    canon_as_of_transaction_time,
    canon_valid_as_of,
    school_membership_as_of,
    weather_as_of,
    why_changed,
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


def _insert_cycle(cfg: AppConfig) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, created_at) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'HERESY', 0.4, 'f', '2024-01-01 00:00:00')"
        )
        conn.commit()
        return int(cur.lastrowid)


def test_repository_round_trips_history_for_a_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle(cfg)
    repo = TemporalCanonRepository(cfg)

    assert repo.has_any_for_cycle(cycle_id) is False

    repo.append(cycle_id, "HERESY", 0.4, valid_from="2024-01-01 00:00:00")
    repo.append(cycle_id, "CANON", 0.9, valid_from="2024-02-01 00:00:00")

    history = repo.history_for_cycle(cycle_id)
    assert [s.verdict for s in history] == ["HERESY", "CANON"]
    assert repo.has_any_for_cycle(cycle_id) is True


def test_canon_valid_as_of_reflects_a_promotion_and_retirement_timeline(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle(cfg)
    repo = TemporalCanonRepository(cfg)

    repo.append(cycle_id, "HERESY", 0.4, valid_from="2024-01-01 00:00:00")
    repo.append(cycle_id, "CANON", 0.9, valid_from="2024-02-01 00:00:00")
    repo.append(cycle_id, "CANON", 0.9, active=False, valid_from="2024-03-01 00:00:00")

    assert canon_valid_as_of(cfg, "2023-12-01 00:00:00") == []
    assert canon_valid_as_of(cfg, "2024-01-15 00:00:00") == []  # HERESY, not active canon
    assert canon_valid_as_of(cfg, "2024-02-15 00:00:00") == [cycle_id]  # rehabilitated to active CANON
    assert canon_valid_as_of(cfg, "2024-04-01 00:00:00") == []  # retired


def test_canon_as_of_cycle_uses_that_cycles_own_created_at(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    older_cycle = _insert_cycle(cfg)
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, created_at) "
            "VALUES ('q2', 'm', 'p', 'r', 0, 'CANON', 0.9, 'f', '2024-02-15 00:00:00')"
        )
        later_cycle = int(cur.lastrowid)
        conn.commit()

    repo = TemporalCanonRepository(cfg)
    repo.append(older_cycle, "HERESY", 0.4, valid_from="2024-01-01 00:00:00")
    repo.append(older_cycle, "CANON", 0.9, valid_from="2024-02-01 00:00:00")

    assert canon_as_of_cycle(cfg, later_cycle) == [older_cycle]


def test_canon_as_of_cycle_raises_for_an_unknown_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with pytest.raises(TemporalNotFoundError):
        canon_as_of_cycle(cfg, 999)


def test_transaction_time_travel_is_independent_of_valid_time_travel_for_a_backfilled_row(tmp_path):
    """
    A backfilled row's recorded_at (when the backfill ran) postdates its
    valid_from (the historical moment being reconstructed) -- the one case
    in this system where the two clocks genuinely diverge.
    """
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle(cfg)

    with connect(cfg.database) as conn:
        conn.execute(
            """
            INSERT INTO canon_temporal_states(cycle_id, verdict, final_score, active, valid_from, recorded_at, creation_method)
            VALUES (?, 'CANON', 0.9, 1, '2024-01-01 00:00:00', '2024-06-01 00:00:00', ?)
            """,
            (cycle_id, CREATION_BACKFILL),
        )
        conn.commit()

    # Valid-time travel: this cycle WAS canon starting 2024-01-01, in the modeled world.
    assert canon_valid_as_of(cfg, "2024-02-01 00:00:00") == [cycle_id]

    # Transaction-time travel: Field Horizon had not yet RECORDED that fact
    # until the backfill ran on 2024-06-01 -- as of 2024-02-01, it knew nothing.
    assert canon_as_of_transaction_time(cfg, "2024-02-01 00:00:00") == []
    assert canon_as_of_transaction_time(cfg, "2024-07-01 00:00:00") == [cycle_id]


def test_why_changed_resolves_to_a_real_event_and_edge(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle(cfg)

    event = EventRepository(cfg).append(
        event_type="FragmentRehabilitated", actor="system", aggregate_type="cycle",
        correlation_id="corr-1", aggregate_id=cycle_id,
    )
    edge = ProvenanceEdgeRepository(cfg).append(OBJECT_COUNCIL, 1, OBJECT_CYCLE, cycle_id, REL_RETIRES)

    TemporalCanonRepository(cfg).append(
        cycle_id, "HERESY", 0.4, valid_from="2024-01-01 00:00:00"
    )
    TemporalCanonRepository(cfg).append(
        cycle_id, "CANON", 0.9, valid_from="2024-02-01 00:00:00",
        source_event_id=event.event_id, source_edge_id=edge.id,
    )

    result = why_changed(cfg, cycle_id)

    assert result.cycle_id == cycle_id
    assert len(result.transitions) == 2

    second = result.transitions[1]
    assert second.from_state is not None
    assert second.from_state.verdict == "HERESY"
    assert second.to_state.verdict == "CANON"
    assert second.event is not None
    assert second.event.event_id == event.event_id
    assert second.edge is not None
    assert second.edge.id == edge.id


def test_why_changed_returns_empty_transitions_for_a_cycle_with_no_history(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle(cfg)

    result = why_changed(cfg, cycle_id)
    assert result.transitions == []


def _insert_canon_event(cfg: AppConfig, cycle_id: int, event: str, detail: dict, created_at: str) -> int:
    import json as _json

    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO canon_events(cycle_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (cycle_id, event, _json.dumps(detail), created_at),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_backfill_reconstructs_the_original_verdict_before_a_rehabilitation(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    # cycles.verdict is CURRENTLY 'CANON' -- set_cycle_verdict overwrote it
    # at rehabilitation, exactly as council.py does; only canon_events
    # remembers it was originally HERESY.
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, created_at) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'f', '2024-01-01 00:00:00')"
        )
        cycle_id = int(cur.lastrowid)
        conn.commit()

    edge = ProvenanceEdgeRepository(cfg).append(OBJECT_COUNCIL, 1, OBJECT_CYCLE, cycle_id, REL_REHABILITATES)
    _insert_canon_event(
        cfg, cycle_id, "REHABILITATED",
        {"old_verdict": "HERESY", "old_score": 0.4, "new_verdict": "CANON", "new_score": 0.9},
        "2024-02-01 00:00:00",
    )

    report = backfill_canon_temporal_states(cfg)

    history = TemporalCanonRepository(cfg).history_for_cycle(cycle_id)
    assert [s.verdict for s in history] == ["HERESY", "CANON"]
    assert history[0].valid_from == "2024-01-01 00:00:00"
    assert history[1].valid_from == "2024-02-01 00:00:00"
    assert history[1].source_edge_id == edge.id
    assert report.cycles_processed == 1
    assert report.states_written == 2
    assert report.unresolved_edges == 0


def test_backfill_reconstructs_a_retired_cycle_with_no_rehabilitation(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, created_at, retired_at) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'f', '2024-01-01 00:00:00', '2024-03-01 00:00:00')"
        )
        cycle_id = int(cur.lastrowid)
        conn.commit()

    ProvenanceEdgeRepository(cfg).append(OBJECT_COUNCIL, 1, OBJECT_CYCLE, cycle_id, REL_RETIRES)
    _insert_canon_event(cfg, cycle_id, "RETIRED", {"reason": "outpaced"}, "2024-03-01 00:00:00")

    backfill_canon_temporal_states(cfg)

    history = TemporalCanonRepository(cfg).history_for_cycle(cycle_id)
    assert [s.verdict for s in history] == ["CANON", "CANON"]
    assert [s.active for s in history] == [True, False]


def test_backfill_counts_unresolved_edges_when_none_exists(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, created_at) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'f', '2024-01-01 00:00:00')"
        )
        cycle_id = int(cur.lastrowid)
        conn.commit()

    _insert_canon_event(
        cfg, cycle_id, "REHABILITATED",
        {"old_verdict": "HERESY", "old_score": 0.4, "new_verdict": "CANON", "new_score": 0.9},
        "2024-02-01 00:00:00",
    )
    # No provenance_edges row exists for this cycle at all.

    report = backfill_canon_temporal_states(cfg)
    assert report.unresolved_edges == 1


def test_backfill_is_idempotent_per_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle(cfg)

    first = backfill_canon_temporal_states(cfg)
    second = backfill_canon_temporal_states(cfg)

    assert first.cycles_processed == 1
    assert second.cycles_processed == 0
    assert len(TemporalCanonRepository(cfg).history_for_cycle(cycle_id)) == 1


def test_backfill_skips_dry_run_cycles(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, created_at) "
            "VALUES ('q', 'm', 'p', 'r', 1, 'CANON', 0.9, 'f', '2024-01-01 00:00:00')"
        )
        cycle_id = int(cur.lastrowid)
        conn.commit()

    backfill_canon_temporal_states(cfg)
    assert TemporalCanonRepository(cfg).history_for_cycle(cycle_id) == []


def test_school_membership_as_of_returns_the_generation_active_at_that_time(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle(cfg)

    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO schools(name, summary, run_at) VALUES ('Gen 1', 's', '2024-01-01 00:00:00')"
        )
        gen1_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO school_members(school_id, cycle_id, distance) VALUES (?, ?, 0.1)", (gen1_id, cycle_id)
        )
        cur = conn.execute(
            "INSERT INTO schools(name, summary, previous_school_id, run_at) VALUES ('Gen 2', 's', ?, '2024-02-01 00:00:00')",
            (gen1_id,),
        )
        gen2_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO school_members(school_id, cycle_id, distance) VALUES (?, ?, 0.1)", (gen2_id, cycle_id)
        )
        conn.commit()

    assert [s.id for s in school_membership_as_of(cfg, "2024-01-15 00:00:00")] == [gen1_id]
    assert [s.id for s in school_membership_as_of(cfg, "2024-02-15 00:00:00")] == [gen2_id]
    assert school_membership_as_of(cfg, "2023-12-01 00:00:00") == []


def test_weather_as_of_reads_the_most_recent_reading_at_or_before_the_cutoff(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO weather_readings(recorded_at, scope, axis, value) VALUES ('2024-01-01 00:00:00', 'canon', 'tawhid', 0.2)"
        )
        conn.execute(
            "INSERT INTO weather_readings(recorded_at, scope, axis, value) VALUES ('2024-02-01 00:00:00', 'canon', 'tawhid', 0.7)"
        )
        conn.commit()

    assert weather_as_of(cfg, "2024-01-15 00:00:00") == {"tawhid": 0.2}
    assert weather_as_of(cfg, "2024-02-15 00:00:00") == {"tawhid": 0.7}
    assert weather_as_of(cfg, "2023-12-01 00:00:00") == {}
