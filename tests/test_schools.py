from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.embeddings import store_cycle_embedding
from fieldhorizon.events import EventRepository
from fieldhorizon.schools import (
    all_schools,
    latest_schools,
    name_school,
    run_schools,
    school_distances_for_run,
    school_members_detail,
    select_k_and_cluster,
    silhouette_score,
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


def _fixed_version():
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("fieldhorizon.embeddings.current_embedding_version", return_value="nomic-embed-text"))
    return stack


def insert_canon_cycle(cfg: AppConfig, query: str, fragment: str, vector: list[float]) -> int:
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


def test_kmeans_clustering_is_deterministic_under_a_fixed_seed():
    rng = np.random.RandomState(0)
    cluster_a = rng.normal(loc=[1, 0, 0, 0], scale=0.05, size=(6, 4))
    cluster_b = rng.normal(loc=[0, 1, 0, 0], scale=0.05, size=(6, 4))
    vectors = np.vstack([cluster_a, cluster_b])

    k1, labels1, _ = select_k_and_cluster(vectors, seed=42)
    k2, labels2, _ = select_k_and_cluster(vectors, seed=42)

    assert k1 == k2
    assert np.array_equal(labels1, labels2)


def test_kmeans_clustering_selects_the_true_number_of_clusters():
    rng = np.random.RandomState(1)
    cluster_a = rng.normal(loc=[1, 0, 0, 0], scale=0.03, size=(6, 4))
    cluster_b = rng.normal(loc=[0, 1, 0, 0], scale=0.03, size=(6, 4))
    vectors = np.vstack([cluster_a, cluster_b])

    k, labels, _ = select_k_and_cluster(vectors, seed=7)

    assert k == 2
    assert len(set(labels[:6].tolist())) == 1
    assert len(set(labels[6:].tolist())) == 1
    assert labels[0] != labels[6]


def test_silhouette_score_returns_negative_one_for_a_single_cluster():
    distance_matrix = np.zeros((4, 4))
    labels = np.zeros(4, dtype=int)
    assert silhouette_score(distance_matrix, labels) == -1.0


def test_name_school_rejects_empty_cluster(tmp_path):
    cfg = make_config(tmp_path)
    with pytest.raises(ValueError, match="no member fragments"):
        name_school(cfg, [])


def test_name_school_parses_llm_response(tmp_path):
    cfg = make_config(tmp_path)
    raw = json.dumps({"name": "The Audit School", "summary": "They believe in the ledger. They fear revelation."})
    with patch("fieldhorizon.schools.call_ollama", return_value=raw):
        result = name_school(cfg, ["a fragment about audits and ledgers"])
    assert result["name"] == "The Audit School"
    assert "ledger" in result["summary"]


def test_name_school_falls_back_to_a_default_name_when_missing(tmp_path):
    cfg = make_config(tmp_path)
    raw = json.dumps({"summary": "some summary"})
    with patch("fieldhorizon.schools.call_ollama", return_value=raw):
        result = name_school(cfg, ["fragment"])
    assert result["name"] == "Unnamed School"


def test_run_schools_returns_empty_below_the_minimum_cycle_count(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    insert_canon_cycle(cfg, "q1", "fragment one", [1.0, 0.0, 0.0, 0.0])

    with _fixed_version():
        results = run_schools(cfg, k="auto")

    assert results == []


def test_run_schools_clusters_and_names_and_persists(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    for i in range(3):
        insert_canon_cycle(cfg, f"q{i}", f"fragment about the machine as idol {i}", [1.0, 0.0, 0.0, 0.0])
    for i in range(3):
        insert_canon_cycle(cfg, f"r{i}", f"fragment about gardens and rivers {i}", [0.0, 1.0, 0.0, 0.0])

    raw = json.dumps({"name": "A School", "summary": "Two sentences. About doctrine."})
    with _fixed_version(), patch("fieldhorizon.schools.call_ollama", return_value=raw):
        results = run_schools(cfg, k=2, seed=42)

    assert len(results) == 2
    total_members = sum(len(r.member_cycle_ids) for r in results)
    assert total_members == 6

    with connect(cfg.database) as conn:
        school_rows = conn.execute("SELECT COUNT(*) AS n FROM schools").fetchone()["n"]
        member_rows = conn.execute("SELECT COUNT(*) AS n FROM school_members").fetchone()["n"]
    assert school_rows == 2
    assert member_rows == 6


def test_run_schools_records_a_run_manifest_with_the_seed(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    for i in range(3):
        insert_canon_cycle(cfg, f"q{i}", f"fragment about the machine as idol {i}", [1.0, 0.0, 0.0, 0.0])
    for i in range(3):
        insert_canon_cycle(cfg, f"r{i}", f"fragment about gardens and rivers {i}", [0.0, 1.0, 0.0, 0.0])

    raw = json.dumps({"name": "A School", "summary": "Two sentences. About doctrine."})
    with _fixed_version(), patch("fieldhorizon.schools.call_ollama", return_value=raw):
        results = run_schools(cfg, k=2, seed=7)

    from fieldhorizon.manifests import RunManifestRepository

    manifests = RunManifestRepository(cfg).recent(operation="schools", limit=5)
    assert len(manifests) == 1
    assert manifests[0].random_seeds == {"seed": 7, "k": 2}
    assert set(manifests[0].output_ids) == {f"school:{r.school_id}" for r in results}


def test_run_schools_records_lineage_to_the_most_overlapping_prior_school(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    for i in range(3):
        insert_canon_cycle(cfg, f"q{i}", f"fragment about the machine as idol {i}", [1.0, 0.0, 0.0, 0.0])
    for i in range(3):
        insert_canon_cycle(cfg, f"r{i}", f"fragment about gardens and rivers {i}", [0.0, 1.0, 0.0, 0.0])

    raw = json.dumps({"name": "A School", "summary": "Two sentences. About doctrine."})
    with _fixed_version(), patch("fieldhorizon.schools.call_ollama", return_value=raw):
        first_run = run_schools(cfg, k=2, seed=42)
        second_run = run_schools(cfg, k=2, seed=42)

    first_ids = {frozenset(r.member_cycle_ids) for r in first_run}
    matches = [r for r in second_run if frozenset(r.member_cycle_ids) in first_ids]
    assert matches
    assert all(r.previous_school_id is not None for r in matches)


def test_run_schools_records_member_of_edges(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    for i in range(3):
        insert_canon_cycle(cfg, f"q{i}", f"fragment about the machine as idol {i}", [1.0, 0.0, 0.0, 0.0])
    for i in range(3):
        insert_canon_cycle(cfg, f"r{i}", f"fragment about gardens and rivers {i}", [0.0, 1.0, 0.0, 0.0])

    raw = json.dumps({"name": "A School", "summary": "Two sentences. About doctrine."})
    with _fixed_version(), patch("fieldhorizon.schools.call_ollama", return_value=raw):
        results = run_schools(cfg, k=2, seed=42)

    from fieldhorizon.provenance import OBJECT_SCHOOL, REL_MEMBER_OF, ProvenanceEdgeRepository

    repo = ProvenanceEdgeRepository(cfg)
    for result in results:
        members = {e.source_id for e in repo.edges_to(OBJECT_SCHOOL, result.school_id, relation_type=REL_MEMBER_OF)}
        assert members == {str(cid) for cid in result.member_cycle_ids}


def test_run_schools_records_a_supersedes_edge_on_reclustering(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    for i in range(3):
        insert_canon_cycle(cfg, f"q{i}", f"fragment about the machine as idol {i}", [1.0, 0.0, 0.0, 0.0])
    for i in range(3):
        insert_canon_cycle(cfg, f"r{i}", f"fragment about gardens and rivers {i}", [0.0, 1.0, 0.0, 0.0])

    raw = json.dumps({"name": "A School", "summary": "Two sentences. About doctrine."})
    with _fixed_version(), patch("fieldhorizon.schools.call_ollama", return_value=raw):
        first_run = run_schools(cfg, k=2, seed=42)
        second_run = run_schools(cfg, k=2, seed=42)

    from fieldhorizon.provenance import OBJECT_SCHOOL, REL_SUPERSEDES, ProvenanceEdgeRepository

    repo = ProvenanceEdgeRepository(cfg)
    superseding = [r for r in second_run if r.previous_school_id is not None]
    assert superseding
    for result in superseding:
        edges = repo.edges_from(OBJECT_SCHOOL, result.school_id, relation_type=REL_SUPERSEDES)
        assert [e.target_id for e in edges] == [str(result.previous_school_id)]
        assert result.previous_school_id in {r.school_id for r in first_run}


def test_latest_schools_returns_only_the_most_recent_run(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    for i in range(3):
        insert_canon_cycle(cfg, f"q{i}", f"fragment about the machine as idol {i}", [1.0, 0.0, 0.0, 0.0])
    for i in range(3):
        insert_canon_cycle(cfg, f"r{i}", f"fragment about gardens and rivers {i}", [0.0, 1.0, 0.0, 0.0])

    raw = json.dumps({"name": "A School", "summary": "Two sentences. About doctrine."})
    with _fixed_version(), patch("fieldhorizon.schools.call_ollama", return_value=raw):
        run_schools(cfg, k=2, seed=42)
        second_run = run_schools(cfg, k=2, seed=42)

    active = latest_schools(cfg)
    assert {s.id for s in active} == {r.school_id for r in second_run}


def test_run_schools_emits_a_watchable_event_chain(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    for i in range(3):
        insert_canon_cycle(cfg, f"q{i}", f"fragment about the machine as idol {i}", [1.0, 0.0, 0.0, 0.0])
    for i in range(3):
        insert_canon_cycle(cfg, f"r{i}", f"fragment about gardens and rivers {i}", [0.0, 1.0, 0.0, 0.0])

    raw = json.dumps({"name": "A School", "summary": "Two sentences. About doctrine."})
    with _fixed_version(), patch("fieldhorizon.schools.call_ollama", return_value=raw):
        results = run_schools(cfg, k=2, seed=42, correlation_id="a-known-run-id")

    events = EventRepository(cfg).by_correlation("a-known-run-id")
    event_types = [e.event_type for e in events]
    assert event_types[0] == "SchoolsStarted"
    assert event_types.count("SchoolNamed") == len(results)
    assert event_types[-1] == "SchoolsCompleted"


def test_run_schools_below_minimum_emits_no_events(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    insert_canon_cycle(cfg, "q1", "fragment one", [1.0, 0.0, 0.0, 0.0])

    with _fixed_version():
        run_schools(cfg, k="auto", correlation_id="a-known-run-id")

    assert EventRepository(cfg).by_correlation("a-known-run-id") == []


def test_all_schools_includes_every_run_not_just_the_latest(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    for i in range(3):
        insert_canon_cycle(cfg, f"q{i}", f"fragment about the machine as idol {i}", [1.0, 0.0, 0.0, 0.0])
    for i in range(3):
        insert_canon_cycle(cfg, f"r{i}", f"fragment about gardens and rivers {i}", [0.0, 1.0, 0.0, 0.0])

    raw = json.dumps({"name": "A School", "summary": "Two sentences. About doctrine."})
    with _fixed_version(), patch("fieldhorizon.schools.call_ollama", return_value=raw):
        first_run = run_schools(cfg, k=2, seed=42)
        second_run = run_schools(cfg, k=2, seed=42)

    all_ids = {s.id for s in all_schools(cfg)}
    assert all_ids == {r.school_id for r in first_run} | {r.school_id for r in second_run}


def test_school_members_detail_sorts_closest_to_centroid_first(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    for i in range(4):
        insert_canon_cycle(cfg, f"q{i}", f"fragment about the machine as idol {i}", [1.0, 0.0, 0.0, 0.0])

    raw = json.dumps({"name": "A School", "summary": "Two sentences. About doctrine."})
    with _fixed_version(), patch("fieldhorizon.schools.call_ollama", return_value=raw):
        results = run_schools(cfg, k=1, seed=42)

    members = school_members_detail(cfg, results[0].school_id)
    assert len(members) == 4
    assert all(members[i].distance <= members[i + 1].distance for i in range(len(members) - 1))
    assert members[0].query.startswith("q")


def test_school_distances_for_run_is_symmetric_and_positive(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    for i in range(3):
        insert_canon_cycle(cfg, f"q{i}", f"fragment about the machine as idol {i}", [1.0, 0.0, 0.0, 0.0])
    for i in range(3):
        insert_canon_cycle(cfg, f"r{i}", f"fragment about gardens and rivers {i}", [0.0, 1.0, 0.0, 0.0])

    raw = json.dumps({"name": "A School", "summary": "Two sentences. About doctrine."})
    with _fixed_version(), patch("fieldhorizon.schools.call_ollama", return_value=raw):
        results = run_schools(cfg, k=2, seed=42)

        with connect(cfg.database) as conn:
            run_at = conn.execute("SELECT run_at FROM schools LIMIT 1").fetchone()["run_at"]

        distances = school_distances_for_run(cfg, run_at)

    assert len(distances) == 1  # exactly one pair for two schools
    assert distances[0].distance > 0
    assert {distances[0].school_a_id, distances[0].school_b_id} == {r.school_id for r in results}
