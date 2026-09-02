from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fieldhorizon.config import AppConfig
from fieldhorizon.council import run_council
from fieldhorizon.cycle import run_cycle
from fieldhorizon.db import init_db
from fieldhorizon.events import ACTOR_CLI, AGGREGATE_CYCLE, EventRepository, OperationEmitter
from fieldhorizon.principal import LOCAL_ADMIN_PRINCIPAL, PrincipalContext


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


def test_operation_emitter_defaults_to_the_local_admin_principal(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    emitter = OperationEmitter(cfg, actor=ACTOR_CLI, aggregate_type=AGGREGATE_CYCLE)
    assert emitter.principal == LOCAL_ADMIN_PRINCIPAL

    event = emitter.emit("SomeEvent")
    assert event.principal_id == LOCAL_ADMIN_PRINCIPAL.principal_id == "local-admin"


def test_operation_emitter_propagates_a_supplied_principal(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    custom = PrincipalContext(principal_id="alice", roles=("editor",))

    emitter = OperationEmitter(cfg, actor=ACTOR_CLI, aggregate_type=AGGREGATE_CYCLE, principal=custom)
    event = emitter.emit("SomeEvent")

    assert event.principal_id == "alice"


def test_event_repository_round_trips_principal_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)

    written = repo.append(
        event_type="SomeEvent", actor=ACTOR_CLI, aggregate_type=AGGREGATE_CYCLE,
        correlation_id="corr-1", principal_id="alice",
    )
    fetched = repo.get(written.event_id)

    assert fetched is not None
    assert fetched.principal_id == "alice"


def test_event_repository_defaults_principal_id_to_none_when_not_supplied(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = EventRepository(cfg)

    written = repo.append(event_type="SomeEvent", actor=ACTOR_CLI, aggregate_type=AGGREGATE_CYCLE, correlation_id="corr-1")
    fetched = repo.get(written.event_id)

    assert fetched is not None
    assert fetched.principal_id is None


def test_run_cycle_propagates_a_supplied_principal_into_its_events(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    custom = PrincipalContext(principal_id="alice", roles=("editor",))

    with (
        patch("fieldhorizon.cycle.search_books", return_value=[]),
        patch("fieldhorizon.cycle.search_json", return_value=[]),
        patch("fieldhorizon.cycle.load_canon_fragments", return_value=[]),
        patch("fieldhorizon.cycle.generate_fragment", return_value="a fragment"),
        patch("fieldhorizon.registries.requests.get", side_effect=RuntimeError("no ollama")),
    ):
        run_cycle(cfg, "a query", dry_run=True, principal=custom)

    events = EventRepository(cfg).recent(limit=20)
    assert events
    assert all(e.principal_id == "alice" for e in events)


def test_run_cycle_defaults_to_the_local_admin_principal_when_none_supplied(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with (
        patch("fieldhorizon.cycle.search_books", return_value=[]),
        patch("fieldhorizon.cycle.search_json", return_value=[]),
        patch("fieldhorizon.cycle.load_canon_fragments", return_value=[]),
        patch("fieldhorizon.cycle.generate_fragment", return_value="a fragment"),
        patch("fieldhorizon.registries.requests.get", side_effect=RuntimeError("no ollama")),
    ):
        run_cycle(cfg, "a query", dry_run=True)

    events = EventRepository(cfg).recent(limit=20)
    assert events
    assert all(e.principal_id == "local-admin" for e in events)


def test_run_council_propagates_a_supplied_principal_into_its_events(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    custom = PrincipalContext(principal_id="alice")

    run_council(cfg, sample=0, principal=custom)

    events = EventRepository(cfg).recent(limit=20)
    assert events
    assert all(e.principal_id == "alice" for e in events)
