from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.embeddings import store_cycle_embedding
from fieldhorizon.manifests import RunManifest, RunManifestRepository
from fieldhorizon.replay import (
    ReplayNotFoundError,
    replay_cycle,
    replay_cycle_against_historical_canon,
    replay_cycle_level2,
    replay_cycle_level3,
    replay_cycle_level5,
    replay_schools_level4,
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


def insert_cycle(cfg: AppConfig, prompt: str, model: str, fragment: str) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q', ?, ?, 'r', 0, 'CANON', 0.9, ?)",
            (model, prompt, fragment),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_replay_raises_for_unknown_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with pytest.raises(ReplayNotFoundError):
        replay_cycle(cfg, 999)


def test_replay_raises_when_cycle_has_no_stored_prompt(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q', 'm', '', 'r', 0, 'CANON', 0.9, 'f')"
        )
        conn.commit()
        cycle_id = int(cur.lastrowid)

    with pytest.raises(ReplayNotFoundError):
        replay_cycle(cfg, cycle_id)


def test_replay_reuses_the_stored_model_by_default(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "the stored prompt", "hermes3:8b", "the original fragment")

    with patch("fieldhorizon.replay.generate_fragment", return_value="a fresh fragment") as mock_gen:
        result = replay_cycle(cfg, cycle_id)

    mock_gen.assert_called_once_with(cfg, "the stored prompt", model="hermes3:8b")
    assert result.model == "hermes3:8b"
    assert result.old_fragment == "the original fragment"
    assert result.new_fragment == "a fresh fragment"


def test_replay_honors_an_explicit_model_override(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "the stored prompt", "hermes3:8b", "the original fragment")

    with patch("fieldhorizon.replay.generate_fragment", return_value="a fresh fragment") as mock_gen:
        result = replay_cycle(cfg, cycle_id, model="qwen3:8b")

    mock_gen.assert_called_once_with(cfg, "the stored prompt", model="qwen3:8b")
    assert result.model == "qwen3:8b"


def test_replay_produces_a_unified_diff_when_fragments_differ(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "prompt", "m", "line one\nline two")

    with patch("fieldhorizon.replay.generate_fragment", return_value="line one\nline three"):
        result = replay_cycle(cfg, cycle_id)

    assert "-line two" in result.diff
    assert "+line three" in result.diff


def test_replay_diff_is_empty_when_fragments_are_identical(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "prompt", "m", "identical text")

    with patch("fieldhorizon.replay.generate_fragment", return_value="identical text"):
        result = replay_cycle(cfg, cycle_id)

    assert result.diff == ""


def test_replay_level2_raises_when_no_manifest_was_recorded(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "prompt", "m", "fragment")

    with pytest.raises(ReplayNotFoundError, match="No run_manifests row"):
        replay_cycle_level2(cfg, cycle_id)


def test_replay_level2_reports_no_drift_when_environment_is_unchanged(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "prompt", "m", "fragment")

    from fieldhorizon.manifests import build_manifest_fingerprints

    fingerprints = build_manifest_fingerprints(cfg)
    RunManifestRepository(cfg).record(
        RunManifest(run_id="corr-1", operation="cycle", output_ids=[cycle_id], **fingerprints)
    )

    result = replay_cycle_level2(cfg, cycle_id)
    assert result.run_id == "corr-1"
    assert result.any_drift is False
    assert all(not d.changed for d in result.drifts)


def test_replay_level2_reports_drift_on_a_changed_field(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "prompt", "m", "fragment")

    from fieldhorizon.manifests import build_manifest_fingerprints

    fingerprints = build_manifest_fingerprints(cfg)
    fingerprints["code_commit"] = "stale0000"
    RunManifestRepository(cfg).record(
        RunManifest(run_id="corr-2", operation="cycle", output_ids=[cycle_id], **fingerprints)
    )

    result = replay_cycle_level2(cfg, cycle_id)
    assert result.any_drift is True
    code_commit_drift = next(d for d in result.drifts if d.field == "code_commit")
    assert code_commit_drift.changed is True
    assert code_commit_drift.recorded == "stale0000"


def test_replay_level3_reconstructs_the_exact_evidence_set(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "prompt", "m", "fragment")

    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (?, 'book', 'ref-1', 'book content')",
            (cycle_id,),
        )
        conn.execute(
            "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (?, 'canon', 'ref-2', 'canon content')",
            (cycle_id,),
        )
        conn.commit()

    result = replay_cycle_level3(cfg, cycle_id)
    assert result.verdict == "CANON"
    assert result.final_score == 0.9
    assert [(s.source_kind, s.ref, s.content) for s in result.sources] == [
        ("book", "ref-1", "book content"),
        ("canon", "ref-2", "canon content"),
    ]


def test_replay_level3_raises_for_unknown_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with pytest.raises(ReplayNotFoundError):
        replay_cycle_level3(cfg, 999)


def _fixed_embedding_version():
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("fieldhorizon.embeddings.current_embedding_version", return_value="nomic-embed-text"))
    return stack


def insert_canon_cycle_with_embedding(cfg: AppConfig, query: str, fragment: str, vector: list[float]) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES (?, 'm', 'p', 'r', 0, 'CANON', 0.9, ?)",
            (query, fragment),
        )
        conn.commit()
        cycle_id = int(cur.lastrowid)
    store_cycle_embedding(cfg, cycle_id, vector, "nomic-embed-text", "nomic-embed-text")
    return cycle_id


def _run_schools_for_replay(cfg: AppConfig, seed: int = 7, k: int | str = 2) -> str:
    import json as _json

    from fieldhorizon.schools import run_schools

    for i in range(3):
        insert_canon_cycle_with_embedding(cfg, f"q{i}", f"fragment about the machine as idol {i}", [1.0, 0.0, 0.0, 0.0])
    for i in range(3):
        insert_canon_cycle_with_embedding(cfg, f"r{i}", f"fragment about gardens and rivers {i}", [0.0, 1.0, 0.0, 0.0])

    raw = _json.dumps({"name": "A School", "summary": "Two sentences. About doctrine."})
    with _fixed_embedding_version(), patch("fieldhorizon.schools.call_ollama", return_value=raw):
        run_schools(cfg, k=k, seed=seed)

    manifests = RunManifestRepository(cfg).recent(operation="schools", limit=1)
    return manifests[0].run_id


def test_replay_schools_level4_reports_an_identical_partition_when_nothing_changed(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    run_id = _run_schools_for_replay(cfg)

    with _fixed_embedding_version():
        result = replay_schools_level4(cfg, run_id)

    assert result.seed == 7
    assert result.k == 2
    assert result.missing_cycle_ids == []
    assert result.identical_partition is True
    assert {frozenset(g) for g in result.original_partition} == {frozenset(g) for g in result.replayed_partition}


def test_replay_schools_level4_raises_for_an_unknown_run_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with pytest.raises(ReplayNotFoundError):
        replay_schools_level4(cfg, "no-such-run")


def test_replay_schools_level4_raises_when_too_many_cycles_have_lost_their_embeddings(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    run_id = _run_schools_for_replay(cfg)

    with connect(cfg.database) as conn:
        conn.execute("DELETE FROM canon_embeddings")
        conn.commit()

    with _fixed_embedding_version(), pytest.raises(ReplayNotFoundError, match="too few"):
        replay_schools_level4(cfg, run_id)


def test_replay_cycle_level5_diffs_baseline_against_a_variant_model(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "the stored prompt", "hermes3:8b", "the original fragment")

    def fake_generate(cfg, prompt, model):
        return "baseline text" if model == "hermes3:8b" else "variant text"

    with patch("fieldhorizon.replay.generate_fragment", side_effect=fake_generate):
        result = replay_cycle_level5(cfg, cycle_id, "qwen3:8b")

    assert result.baseline_model == "hermes3:8b"
    assert result.variant_model == "qwen3:8b"
    assert result.baseline_fragment == "baseline text"
    assert result.variant_fragment == "variant text"
    assert "-baseline text" in result.diff
    assert "+variant text" in result.diff


def test_replay_cycle_level5_raises_for_unknown_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with pytest.raises(ReplayNotFoundError):
        replay_cycle_level5(cfg, 999, "qwen3:8b")


def _insert_canon_cycle_with_temporal_state(cfg: AppConfig, fragment: str, valid_from: str) -> int:
    from fieldhorizon.temporal import TemporalCanonRepository

    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, created_at) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, ?, ?)",
            (fragment, valid_from),
        )
        cycle_id = int(cur.lastrowid)
        conn.commit()

    TemporalCanonRepository(cfg).append(cycle_id, "CANON", 0.9, valid_from=valid_from)
    return cycle_id


def test_replay_cycle_against_historical_canon_diffs_two_fresh_generations(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    early_id = _insert_canon_cycle_with_temporal_state(cfg, "early canon fragment", "2024-01-01 00:00:00")
    late_id = _insert_canon_cycle_with_temporal_state(cfg, "late canon fragment", "2024-03-01 00:00:00")
    cycle_id = insert_cycle(cfg, "the stored prompt", "hermes3:8b", "the original fragment")

    def fake_generate(cfg, prompt, model):
        return "generation with late canon" if "late canon fragment" in prompt else "generation without late canon"

    with patch("fieldhorizon.replay.generate_fragment", side_effect=fake_generate):
        result = replay_cycle_against_historical_canon(cfg, cycle_id, as_of="2024-02-01 00:00:00")

    assert result.historical_canon_cycle_ids == [early_id]
    assert set(result.current_canon_cycle_ids) == {early_id, late_id}
    assert result.historical_canon_fragment == "generation without late canon"
    assert result.current_canon_fragment == "generation with late canon"
    assert "-generation with late canon" in result.diff
    assert "+generation without late canon" in result.diff


def test_replay_cycle_against_historical_canon_raises_for_unknown_cycle(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with pytest.raises(ReplayNotFoundError):
        replay_cycle_against_historical_canon(cfg, 999, as_of="2024-01-01 00:00:00")
