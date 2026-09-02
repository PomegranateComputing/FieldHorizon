from __future__ import annotations

from pathlib import Path

from fieldhorizon.config import AppConfig
from fieldhorizon.db import init_db
from fieldhorizon.events import EventRepository, export_jsonl, new_correlation_id, query_events


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


def test_repository_has_no_update_or_delete_methods():
    """Append-only is enforced by the interface's shape, not a DB trigger."""
    public_methods = {name for name in dir(EventRepository) if not name.startswith("_")}
    assert public_methods == {"append", "get", "by_correlation", "by_run", "by_aggregate", "recent"}


def test_append_and_get_round_trips(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)

    correlation_id = new_correlation_id()
    event = repo.append(
        event_type="CycleStarted",
        actor="cli",
        aggregate_type="cycle",
        correlation_id=correlation_id,
        payload={"query": "a query"},
    )

    fetched = repo.get(event.event_id)
    assert fetched is not None
    assert fetched.event_type == "CycleStarted"
    assert fetched.actor == "cli"
    assert fetched.correlation_id == correlation_id
    assert fetched.run_id == correlation_id  # Phase A: run_id defaults to correlation_id
    assert fetched.payload == {"query": "a query"}
    assert fetched.aggregate_id is None
    assert fetched.id is not None


def test_get_returns_none_for_unknown_event_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert EventRepository(cfg).get("not-a-real-id") is None


def test_events_are_returned_in_insertion_order(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)
    correlation_id = new_correlation_id()

    first = repo.append(event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id=correlation_id)
    second = repo.append(
        event_type="RetrievalCompleted", actor="cli", aggregate_type="cycle",
        correlation_id=correlation_id, causation_id=first.event_id,
    )
    repo.append(
        event_type="CycleCompleted", actor="cli", aggregate_type="cycle",
        correlation_id=correlation_id, causation_id=second.event_id, aggregate_id=42,
    )

    ordered = repo.by_correlation(correlation_id)
    assert [e.event_type for e in ordered] == ["CycleStarted", "RetrievalCompleted", "CycleCompleted"]
    assert ordered[2].aggregate_id == "42"


def test_causation_chain_links_consecutive_events(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)
    correlation_id = new_correlation_id()

    first = repo.append(event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id=correlation_id)
    second = repo.append(
        event_type="CycleCompleted", actor="cli", aggregate_type="cycle",
        correlation_id=correlation_id, causation_id=first.event_id,
    )

    assert second.causation_id == first.event_id
    fetched_second = repo.get(second.event_id)
    assert fetched_second is not None
    assert fetched_second.causation_id == first.event_id


def test_by_run_and_by_aggregate_filter_correctly(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)

    run_a = new_correlation_id()
    run_b = new_correlation_id()
    repo.append(event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id=run_a, aggregate_id=1)
    repo.append(event_type="CycleCompleted", actor="cli", aggregate_type="cycle", correlation_id=run_a, aggregate_id=1)
    repo.append(event_type="CycleStarted", actor="server", aggregate_type="cycle", correlation_id=run_b, aggregate_id=2)

    assert len(repo.by_run(run_a)) == 2
    assert len(repo.by_run(run_b)) == 1
    assert len(repo.by_aggregate("cycle", 1)) == 2
    assert len(repo.by_aggregate("cycle", 2)) == 1
    assert repo.by_aggregate("cycle", 999) == []


def test_recent_returns_newest_first(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)
    repo.append(event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id=new_correlation_id())
    last = repo.append(event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id=new_correlation_id())

    assert repo.recent(limit=1)[0].event_id == last.event_id


def test_payload_serialization_round_trips_nested_structures(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)

    payload = {
        "str": "value",
        "int": 42,
        "float": 0.75,
        "bool": True,
        "none": None,
        "nested": {"a": [1, 2, {"b": "c"}]},
    }
    event = repo.append(
        event_type="EvaluationCompleted", actor="cli", aggregate_type="cycle",
        correlation_id=new_correlation_id(), payload=payload,
    )

    fetched = repo.get(event.event_id)
    assert fetched is not None
    assert fetched.payload == payload


def test_export_jsonl_writes_one_line_per_event(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)
    repo.append(event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id=new_correlation_id())
    repo.append(event_type="CycleCompleted", actor="cli", aggregate_type="cycle", correlation_id=new_correlation_id())

    out_path = tmp_path / "events.jsonl"
    count = export_jsonl(cfg, out_path)

    assert count == 2
    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_init_db_is_idempotent_against_a_copy_of_the_real_db(tmp_path):
    real_db = Path(__file__).resolve().parent.parent / "data" / "field_horizon.sqlite3"
    if not real_db.exists():
        return  # nothing to verify against in this environment

    import shutil

    copy_path = tmp_path / "real_copy.sqlite3"
    shutil.copy(real_db, copy_path)

    init_db(copy_path)
    init_db(copy_path)  # second call must not error or duplicate anything

    import sqlite3

    conn = sqlite3.connect(copy_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "domain_events" in tables
    conn.close()


def test_query_events_filters_by_run_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)
    repo.append(event_type="A", actor="cli", aggregate_type="cycle", correlation_id="corr-1")
    repo.append(event_type="B", actor="cli", aggregate_type="cycle", correlation_id="corr-2")

    results = query_events(cfg, run_id="corr-1")
    assert [e.event_type for e in results] == ["A"]


def test_query_events_filters_by_aggregate_id_and_event_type(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)
    repo.append(event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id="c", aggregate_id=1)
    repo.append(event_type="CycleCompleted", actor="cli", aggregate_type="cycle", correlation_id="c", aggregate_id=1)
    repo.append(event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id="c", aggregate_id=2)

    results = query_events(cfg, aggregate_id=1, event_type="CycleStarted")
    assert len(results) == 1
    assert results[0].aggregate_id == "1"


def test_query_events_since_id_returns_only_later_events(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)
    first = repo.append(event_type="A", actor="cli", aggregate_type="cycle", correlation_id="c")
    repo.append(event_type="B", actor="cli", aggregate_type="cycle", correlation_id="c")

    all_events = query_events(cfg, run_id="c")
    first_id = next(e.id for e in all_events if e.event_id == first.event_id)

    results = query_events(cfg, since_id=first_id)
    assert [e.event_type for e in results] == ["B"]


def test_query_events_respects_limit(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)
    for i in range(5):
        repo.append(event_type=f"E{i}", actor="cli", aggregate_type="cycle", correlation_id="c")

    results = query_events(cfg, limit=2)
    assert len(results) == 2
    assert [e.event_type for e in results] == ["E0", "E1"]  # ascending order, oldest first
