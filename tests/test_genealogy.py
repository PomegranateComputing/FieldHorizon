from __future__ import annotations

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.events import EventRepository
from fieldhorizon.genealogy import (
    EVENT_COUNCIL_UPHELD,
    EVENT_PROMOTED,
    EVENT_RETIRED,
    load_canon_events,
    parent_cycle_ids_for,
    record_canon_event,
    retire_cycle,
    set_cycle_verdict,
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


def insert_cycle(cfg: AppConfig, verdict: str = "CANON", final_score: float = 0.9) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            """
            INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment)
            VALUES ('q', 'test-model', 'prompt', 'response', 0, ?, ?, 'fragment')
            """,
            (verdict, final_score),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)


def test_record_and_load_canon_events_round_trip(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg)

    record_canon_event(cfg, cycle_id, EVENT_PROMOTED, {"final_score": 0.9})
    record_canon_event(cfg, cycle_id, EVENT_RETIRED, {"reason": "test"})

    events = load_canon_events(cfg, cycle_id)

    assert [e["event"] for e in events] == [EVENT_PROMOTED, EVENT_RETIRED]
    assert events[0]["detail"] == {"final_score": 0.9}
    assert events[1]["detail"] == {"reason": "test"}


def test_record_canon_event_cross_emits_a_mapped_domain_event(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg)

    domain_event = record_canon_event(cfg, cycle_id, EVENT_PROMOTED, {"final_score": 0.9})

    assert domain_event is not None
    assert domain_event.event_type == "FragmentPromoted"
    assert domain_event.aggregate_type == "cycle"
    assert domain_event.aggregate_id == str(cycle_id)
    assert domain_event.payload == {"final_score": 0.9}

    with connect(cfg.database) as conn:
        canon_row = conn.execute(
            "SELECT id, event FROM canon_events WHERE cycle_id = ? AND event = ?", (cycle_id, EVENT_PROMOTED)
        ).fetchone()
    assert domain_event.canon_event_id == canon_row["id"]


def test_record_canon_event_maps_council_events_correctly(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg)

    domain_event = record_canon_event(cfg, cycle_id, EVENT_COUNCIL_UPHELD, {})
    assert domain_event is not None
    assert domain_event.event_type == "CouncilUpheld"


def test_record_canon_event_honors_a_supplied_correlation_and_causation_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg)

    domain_event = record_canon_event(
        cfg, cycle_id, EVENT_PROMOTED, {}, actor="cli", correlation_id="corr-123", causation_id="cause-456",
    )

    assert domain_event is not None
    assert domain_event.correlation_id == "corr-123"
    assert domain_event.causation_id == "cause-456"
    assert domain_event.actor == "cli"


def test_record_canon_event_still_writes_canon_events_row_unchanged(tmp_path):
    """The canon_events table itself is untouched by the cross-emission -- same schema, same INSERT."""
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg)

    record_canon_event(cfg, cycle_id, EVENT_PROMOTED, {"final_score": 0.9})

    events = load_canon_events(cfg, cycle_id)
    assert [e["event"] for e in events] == [EVENT_PROMOTED]
    assert events[0]["detail"] == {"final_score": 0.9}


def test_record_canon_event_cross_emission_is_findable_via_event_repository(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg)

    record_canon_event(cfg, cycle_id, EVENT_PROMOTED, {})

    events = EventRepository(cfg).by_aggregate("cycle", cycle_id)
    assert len(events) == 1
    assert events[0].event_type == "FragmentPromoted"


def test_load_canon_events_returns_empty_list_with_no_history(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg)

    assert load_canon_events(cfg, cycle_id) == []


def test_set_cycle_verdict_updates_verdict_and_score_only(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, verdict="HERESY", final_score=0.5)

    set_cycle_verdict(cfg, cycle_id, "CANON", 0.9)

    with connect(cfg.database) as conn:
        row = conn.execute(
            "SELECT verdict, final_score, response, fragment FROM cycles WHERE id = ?", (cycle_id,)
        ).fetchone()

    assert row["verdict"] == "CANON"
    assert row["final_score"] == 0.9
    # The prose is never touched by a re-judgment -- only verdict/score.
    assert row["response"] == "response"
    assert row["fragment"] == "fragment"


def test_retire_cycle_sets_retired_at_and_reason_without_deleting(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg)

    retire_cycle(cfg, cycle_id, "no longer clears canon")

    with connect(cfg.database) as conn:
        row = conn.execute(
            "SELECT retired_at, retirement_reason, verdict FROM cycles WHERE id = ?", (cycle_id,)
        ).fetchone()

    assert row["retired_at"] is not None
    assert row["retirement_reason"] == "no longer clears canon"
    # Retirement doesn't change the verdict field itself -- it's a
    # separate axis (canon.py excludes on retired_at, not verdict).
    assert row["verdict"] == "CANON"


def test_parent_cycle_ids_for_extracts_cycle_ids_from_canon_rows():
    canon_rows = [
        {"cycle_id": 3, "ref": "canon / cycle_3", "content": "x", "path": "cycles.id=3"},
        {"cycle_id": 7, "ref": "canon / cycle_7", "content": "y", "path": "cycles.id=7"},
    ]
    assert parent_cycle_ids_for(canon_rows) == [3, 7]


def test_parent_cycle_ids_for_ignores_rows_without_a_cycle_id():
    assert parent_cycle_ids_for([{"ref": "canon / legacy"}]) == []


def test_parent_cycle_ids_for_empty_list():
    assert parent_cycle_ids_for([]) == []
