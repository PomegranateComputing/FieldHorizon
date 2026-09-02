from __future__ import annotations

import math
import random
from pathlib import Path
from unittest.mock import patch

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.embeddings import store_cycle_embedding
from fieldhorizon.weather import (
    SCOPE_CANON,
    SCOPE_SURFACE,
    axis_history,
    axis_reading,
    compute_axis_vector,
    corpus_weather,
    get_axis_vector,
    record_council_weather,
    record_cycle_weather,
    school_weather_profile,
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


def _fake_embed(cfg, text):
    # Deterministic, text-dependent vector -- distinguishes positive from
    # negative anchors (different text -> different vector) without a real
    # network call, so axis vectors come out non-degenerate.
    rng = random.Random(sum(ord(c) for c in text) % 997)
    return [rng.uniform(-1, 1) for _ in range(8)]


def _fixed_version():
    # weather.py and embeddings.py each import current_embedding_version into
    # their own namespace (load_cycle_embedding is embeddings.py's own
    # version-match check), so both bindings need patching.
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("fieldhorizon.weather.current_embedding_version", return_value="nomic-embed-text"))
    stack.enter_context(patch("fieldhorizon.embeddings.current_embedding_version", return_value="nomic-embed-text"))
    return stack


def insert_cycle(cfg: AppConfig, query: str, verdict: str, fragment: str, retired: bool = False) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES (?, 'm', 'p', 'r', 0, ?, 0.9, ?)",
            (query, verdict, fragment),
        )
        conn.commit()
        cycle_id = int(cur.lastrowid)

    if retired:
        with connect(cfg.database) as conn:
            conn.execute("UPDATE cycles SET retired_at = CURRENT_TIMESTAMP WHERE id = ?", (cycle_id,))
            conn.commit()

    return cycle_id


def test_compute_axis_vector_is_unit_normalized(tmp_path):
    cfg = make_config(tmp_path)
    with patch("fieldhorizon.weather.embed_text", side_effect=_fake_embed):
        vector = compute_axis_vector(cfg, "hierarchy")

    norm = math.sqrt(sum(x * x for x in vector))
    assert abs(norm - 1.0) < 1e-6


def test_compute_axis_vector_rejects_unknown_axis(tmp_path):
    cfg = make_config(tmp_path)
    with patch("fieldhorizon.weather.embed_text", side_effect=_fake_embed):
        try:
            compute_axis_vector(cfg, "not_a_real_axis")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_get_axis_vector_is_cached_per_embedding_version(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with _fixed_version(), patch("fieldhorizon.weather.embed_text", side_effect=_fake_embed) as mock_embed:
        first = get_axis_vector(cfg, "hierarchy")
        calls_after_first = mock_embed.call_count
        second = get_axis_vector(cfg, "hierarchy")

    assert calls_after_first > 0
    assert mock_embed.call_count == calls_after_first  # no new calls on cache hit
    # Compared approximately: the cached value round-trips through a
    # float32 BLOB (storage_vectors' vector_to_blob), so it isn't bit-exact
    # with the freshly computed float64 python list.
    assert all(abs(a - b) < 1e-6 for a, b in zip(first, second, strict=True))


def test_get_axis_vector_recomputes_for_a_different_embedding_version(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with patch("fieldhorizon.weather.embed_text", side_effect=_fake_embed) as mock_embed:
        with patch("fieldhorizon.weather.current_embedding_version", return_value="v1"):
            get_axis_vector(cfg, "hierarchy")
        calls_after_v1 = mock_embed.call_count

        with patch("fieldhorizon.weather.current_embedding_version", return_value="v2"):
            get_axis_vector(cfg, "hierarchy")

    assert mock_embed.call_count > calls_after_v1


def test_axis_reading_is_cosine_similarity():
    assert abs(axis_reading([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-6
    assert abs(axis_reading([1.0, 0.0], [0.0, 1.0])) < 1e-9


def test_record_cycle_weather_writes_a_surface_reading_per_axis(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "HERESY", "some fragment")

    with _fixed_version(), patch("fieldhorizon.weather.embed_text", side_effect=_fake_embed):
        count = record_cycle_weather(cfg, cycle_id, "some fragment")

    with connect(cfg.database) as conn:
        rows = conn.execute(
            "SELECT axis, scope, cycle_id FROM weather_readings WHERE cycle_id = ?", (cycle_id,)
        ).fetchall()

    assert count == 8  # one per weather axis declared in ontology.yaml
    assert len(rows) == 8
    assert all(row["scope"] == SCOPE_SURFACE for row in rows)


def test_record_cycle_weather_is_best_effort_on_embedding_failure(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "HERESY", "some fragment")

    with patch("fieldhorizon.weather.embed_text", side_effect=RuntimeError("no model")):
        count = record_cycle_weather(cfg, cycle_id, "some fragment")

    assert count == 0
    with connect(cfg.database) as conn:
        rows = conn.execute("SELECT * FROM weather_readings WHERE cycle_id = ?", (cycle_id,)).fetchall()
    assert rows == []


def test_corpus_weather_canon_scope_omits_axes_with_no_data(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    # No CANON cycles at all -> nothing to aggregate.
    with _fixed_version():
        readings = corpus_weather(cfg, scope=SCOPE_CANON)
    assert readings == {}


def test_corpus_weather_canon_scope_averages_active_canon_embeddings(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "CANON", "a canon fragment")
    store_cycle_embedding(cfg, cycle_id, [1.0] * 8, "nomic-embed-text", "nomic-embed-text")

    with _fixed_version(), patch("fieldhorizon.weather.embed_text", side_effect=_fake_embed):
        readings = corpus_weather(cfg, scope=SCOPE_CANON)

    assert set(readings.keys()) == {
        "hierarchy", "entropy", "sacrifice", "transcendence",
        "rationality", "violence_of_rhetoric", "cooperation", "individualism",
    }


def test_corpus_weather_canon_scope_excludes_retired_cycles(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    retired_id = insert_cycle(cfg, "q", "CANON", "a retired fragment", retired=True)
    store_cycle_embedding(cfg, retired_id, [1.0] * 8, "nomic-embed-text", "nomic-embed-text")

    with _fixed_version():
        readings = corpus_weather(cfg, scope=SCOPE_CANON)

    assert readings == {}


def test_record_council_weather_writes_canon_scope_readings(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "CANON", "a canon fragment")
    store_cycle_embedding(cfg, cycle_id, [1.0] * 8, "nomic-embed-text", "nomic-embed-text")

    with connect(cfg.database) as conn:
        cur = conn.execute("INSERT INTO councils(started_at, examined, overturned) VALUES ('now', 0, 0)")
        conn.commit()
        council_id = int(cur.lastrowid)

    with _fixed_version(), patch("fieldhorizon.weather.embed_text", side_effect=_fake_embed):
        count = record_council_weather(cfg, council_id)

    assert count == 8
    with connect(cfg.database) as conn:
        rows = conn.execute(
            "SELECT axis FROM weather_readings WHERE council_id = ? AND scope = ?", (council_id, SCOPE_CANON)
        ).fetchall()
    assert len(rows) == 8


def test_school_weather_profile_averages_the_given_cycles_embeddings(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "CANON", "a school member fragment")
    store_cycle_embedding(cfg, cycle_id, [1.0] * 8, "nomic-embed-text", "nomic-embed-text")

    with _fixed_version(), patch("fieldhorizon.weather.embed_text", side_effect=_fake_embed):
        profile = school_weather_profile(cfg, [cycle_id])

    assert set(profile.keys()) == {
        "hierarchy", "entropy", "sacrifice", "transcendence",
        "rationality", "violence_of_rhetoric", "cooperation", "individualism",
    }


def test_school_weather_profile_is_empty_for_cycles_with_no_stored_embedding(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "CANON", "no embedding stored")

    with _fixed_version():
        profile = school_weather_profile(cfg, [cycle_id])

    assert profile == {}


def test_axis_history_returns_oldest_first(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cycle_id = insert_cycle(cfg, "q", "HERESY", "f")

    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO weather_readings(recorded_at, scope, axis, value, cycle_id) VALUES ('2020-01-01', ?, 'hierarchy', 0.1, ?)",
            (SCOPE_SURFACE, cycle_id),
        )
        conn.execute(
            "INSERT INTO weather_readings(recorded_at, scope, axis, value, cycle_id) VALUES ('2020-01-02', ?, 'hierarchy', 0.5, ?)",
            (SCOPE_SURFACE, cycle_id),
        )
        conn.commit()

    histories = {h.axis: h.values for h in axis_history(cfg, SCOPE_SURFACE, limit=10)}
    assert histories["hierarchy"] == [0.1, 0.5]
