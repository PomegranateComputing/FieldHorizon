from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.engine import FieldHorizonEngine


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


def test_multi_cycle_delegates_to_run_multi_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.engine.run_multi_cycle", return_value=7) as mock_run:
        result = engine.multi_cycle("a query", model="m", dry_run=True)

    assert result == 7
    mock_run.assert_called_once_with(
        cfg, "a query", model="m", agent_model=None, synthesizer_model=None, critic_model=None,
        rewrite_model=None, interpreter_model=None, dry_run=True, auto_rewrite=True, json_domain=None,
        actor="cli", correlation_id=None,
    )


def test_dream_delegates_to_run_dream(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.engine.run_dream", return_value="a-dream-run") as mock_run:
        result = engine.dream(5, 2.0, seed=42)

    assert result == "a-dream-run"
    mock_run.assert_called_once_with(cfg, 5, 2.0, seed=42, model=None, critic_model=None)


def test_proposals_delegates_to_list_proposals(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.engine.list_proposals", return_value=[]) as mock_list:
        assert engine.proposals() == []
    mock_list.assert_called_once_with(cfg)


def test_proposal_apply_delegates_to_apply_proposal(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.engine.apply_proposal", return_value=Path("applied.yaml")) as mock_apply:
        result = engine.proposal_apply(3)

    assert result == Path("applied.yaml")
    mock_apply.assert_called_once_with(cfg, 3, actor="cli")


def test_registry_list_delegates_to_list_registry(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.engine.list_registry", return_value=[{"id": "m1"}]) as mock_list:
        assert engine.registry_list("model") == [{"id": "m1"}]
    mock_list.assert_called_once_with(cfg, "model")


def test_replay_delegates_to_replay_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.engine.replay_cycle", return_value="result") as mock_replay:
        assert engine.replay(5, model="qwen3:8b") == "result"
    mock_replay.assert_called_once_with(cfg, 5, model="qwen3:8b")


def test_replay_variant_delegates_to_replay_cycle_level5(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.engine.replay_cycle_level5", return_value="result") as mock_replay:
        assert engine.replay_variant(5, "qwen3:8b") == "result"
    mock_replay.assert_called_once_with(cfg, 5, "qwen3:8b")


def test_provenance_backfill_delegates(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.engine.backfill_provenance_edges", return_value="report") as mock_backfill:
        assert engine.provenance_backfill() == "report"
    mock_backfill.assert_called_once_with(cfg)


def test_canon_backfill_delegates(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.engine._backfill_canon_temporal_states", return_value="report") as mock_backfill:
        assert engine.canon_backfill() == "report"
    mock_backfill.assert_called_once_with(cfg)


def test_events_delegates_to_query_events_mapping_cycle_id_to_aggregate_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.engine.query_events", return_value=[]) as mock_query:
        assert engine.events(run_id="r1", cycle_id=5, event_type="CycleStarted", since_id=10, limit=50) == []
    mock_query.assert_called_once_with(
        cfg, run_id="r1", aggregate_id=5, event_type="CycleStarted", since_id=10, limit=50
    )


def test_event_delegates_to_event_repository_get(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.events.EventRepository.get", return_value=None) as mock_get:
        assert engine.event("evt-1") is None
    mock_get.assert_called_once_with("evt-1")


def test_ingest_books_delegates(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)

    with patch("fieldhorizon.engine.ingest_books", return_value=3) as mock_ingest:
        assert engine.ingest_books() == 3
    mock_ingest.assert_called_once_with(cfg)


def _insert_source_and_chunks(cfg: AppConfig) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO sources(title, path, source_type, language, weight) VALUES ('Title', '/p', 'book', 'en', 1.0)"
        )
        source_id = int(cur.lastrowid)
        for i in range(2):
            conn.execute(
                "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate, char_start, char_end) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                (source_id, i, f"ref-{i}", f"content {i}", i * 10, i * 10 + 5),
            )
        conn.commit()
    return source_id


def test_sources_returns_real_rows_with_chunk_counts(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    source_id = _insert_source_and_chunks(cfg)
    engine = FieldHorizonEngine(cfg)

    [entry] = engine.sources()
    assert entry.id == source_id
    assert entry.title == "Title"
    assert entry.chunk_count == 2


def _insert_n_sources(cfg: AppConfig, n: int) -> None:
    with connect(cfg.database) as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO sources(title, path, source_type, language, weight) VALUES (?, ?, 'book', 'en', 1.0)",
                (f"Source {i:03d}", f"/p{i:03d}"),
            )
        conn.commit()


def test_sources_respects_limit_and_offset(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_n_sources(cfg, 10)
    engine = FieldHorizonEngine(cfg)

    page = engine.sources(limit=3, offset=2)
    assert [s.title for s in page] == ["Source 002", "Source 003", "Source 004"]
    assert engine.sources_count() == 10


def test_sources_clamps_an_oversized_limit(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_n_sources(cfg, 3)
    engine = FieldHorizonEngine(cfg)

    assert len(engine.sources(limit=10_000_000)) == 3


def test_sources_filters_by_query_substring(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    _insert_n_sources(cfg, 3)
    engine = FieldHorizonEngine(cfg)

    assert [s.title for s in engine.sources(q="002")] == ["Source 002"]
    assert engine.sources_count(q="002") == 1


def test_source_returns_none_for_unknown_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)
    assert engine.source(999) is None


def test_source_chunks_returns_ordered_chunk_entries(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    source_id = _insert_source_and_chunks(cfg)
    engine = FieldHorizonEngine(cfg)

    chunks = engine.source_chunks(source_id)
    assert [c.chunk_index for c in chunks] == [0, 1]
    assert chunks[0].canonical_ref == "ref-0"
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == 5


def _insert_cycle_with_evidence(cfg: AppConfig) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, parent_cycle_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("a query", "hermes3:8b", "prompt text", "response text", 0, "CANON", 0.87, "the resulting fragment", "[1, 2]"),
        )
        cycle_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (?, ?, ?, ?)",
            (cycle_id, "book", "ref-1", "verbatim evidence text"),
        )
        conn.commit()
        return cycle_id


def test_cycle_detail_returns_the_full_stored_row(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle_with_evidence(cfg)
    engine = FieldHorizonEngine(cfg)

    detail = engine.cycle_detail(cycle_id)
    assert detail is not None
    assert detail.query == "a query"
    assert detail.model == "hermes3:8b"
    assert detail.verdict == "CANON"
    assert detail.final_score == 0.87
    assert detail.fragment == "the resulting fragment"
    assert detail.parent_cycle_ids == [1, 2]


def test_cycle_detail_returns_none_for_unknown_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)
    assert engine.cycle_detail(999) is None


def test_cycle_evidence_returns_the_verbatim_persisted_rows(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = _insert_cycle_with_evidence(cfg)
    engine = FieldHorizonEngine(cfg)

    evidence = engine.cycle_evidence(cycle_id)
    assert len(evidence) == 1
    assert evidence[0].source_kind == "book"
    assert evidence[0].ref == "ref-1"
    assert evidence[0].content == "verbatim evidence text"


def test_cycle_evidence_returns_empty_list_for_a_cycle_with_no_recorded_sources(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)
    assert engine.cycle_evidence(999) == []


def test_config_reads_the_real_config_yaml(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    (tmp_path / "config.yaml").write_text("field_horizon:\n  tone: dark\n", encoding="utf-8")
    engine = FieldHorizonEngine(cfg)

    assert engine.config() == {"field_horizon": {"tone": "dark"}}


def test_config_returns_empty_dict_when_no_config_yaml_exists(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)
    assert engine.config() == {}
