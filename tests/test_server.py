from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import fieldhorizon
from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.diagnostics import HealthCheck
from fieldhorizon.server import HOST, create_app, load_or_create_token

TOKEN = "test-token-abc123"

ALL_ROUTES = [
    ("GET", "/status"),
    ("GET", "/system-info"),
    ("GET", "/config"),
    ("GET", "/canon"),
    ("GET", "/cycles/1"),
    ("GET", "/cycles/1/evidence"),
    ("GET", "/why-changed/1"),
    ("GET", "/lineage/1"),
    ("GET", "/provenance/1"),
    ("GET", "/weather"),
    ("GET", "/schools"),
    ("GET", "/schools/all"),
    ("GET", "/schools/1/members"),
    ("GET", "/schools/distances?run_at=2020-01-01"),
    ("GET", "/councils"),
    ("GET", "/councils/1"),
    ("GET", "/sources"),
    ("GET", "/sources/1"),
    ("GET", "/sources/1/chunks"),
    ("GET", "/registry/model"),
    ("GET", "/events"),
    ("GET", "/proposals"),
    ("GET", "/dreams"),
    ("GET", "/semantics/top?kind=domain"),
    ("GET", "/semantics/neighbors?kind=domain&label=tawhid"),
    ("GET", "/capabilities"),
    ("GET", "/health"),
    ("GET", "/fingerprint/1"),
    ("GET", "/export/factions"),
    ("GET", "/export/doctrines"),
    ("GET", "/export/contradictions"),
    ("GET", "/export/weather"),
    ("GET", "/export/beliefs"),
    ("GET", "/metrics"),
]


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


def make_client(tmp_path: Path) -> TestClient:
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    app = create_app(cfg, TOKEN)
    return TestClient(app)


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_every_get_route_requires_auth(tmp_path):
    client = make_client(tmp_path)
    for method, path in ALL_ROUTES:
        response = client.request(method, path)
        assert response.status_code == 401, f"{method} {path} did not require auth (got {response.status_code})"


def test_wrong_token_is_rejected(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/status", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_correct_token_is_accepted_on_every_route(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'a fragment')"
        )
        conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('t', 'p', 'book')")
        conn.execute(
            "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
            "VALUES (1, 0, 'ref', 'content', 1)"
        )
        conn.commit()

    from fieldhorizon.storage_vectors import VectorStore

    with (
        patch("fieldhorizon.embeddings.current_embedding_version", return_value="nomic-embed-text"),
        patch("fieldhorizon.fingerprint.current_embedding_version", return_value="nomic-embed-text"),
    ):
        VectorStore(cfg, "chunk", backend="blob").upsert(1, [1.0, 0.0], "nomic-embed-text")

        from fieldhorizon.server import create_app

        client = TestClient(create_app(cfg, TOKEN))
        for method, path in ALL_ROUTES:
            response = client.request(method, path, headers=auth_headers())
            assert response.status_code == 200, f"{method} {path} -> {response.status_code}: {response.text}"


def test_status_response_matches_contract_shape(tmp_path):
    client = make_client(tmp_path)
    body = client.get("/status", headers=auth_headers()).json()
    assert body["schema_version"] == 1
    assert set(body) >= {
        "cycles_total", "canon_count", "heresy_count", "useful_fragment_count",
        "noise_count", "sources_count", "chunks_count", "councils_count",
        "schools_count", "dream_runs_count",
    }


def test_system_info_reflects_the_real_config(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    client = TestClient(create_app(cfg, TOKEN))
    body = client.get("/system-info", headers=auth_headers()).json()
    assert body["schema_version"] == 1
    assert body["default_model"] == cfg.default_model
    assert body["embedding_model"] == cfg.embedding_model
    assert body["ollama_base_url"] == cfg.ollama_base_url
    assert body["mode"] == cfg.mode
    assert body["tone"] == cfg.tone
    assert body["database_path"] == str(cfg.database)
    assert body["version"] == fieldhorizon.__version__
    assert body["default_weights"] == {
        "vector_similarity": cfg.retrieval_weights.vector_similarity,
        "bm25": cfg.retrieval_weights.bm25,
        "domain_prior": cfg.retrieval_weights.domain_prior,
        "source_weight": cfg.retrieval_weights.source_weight,
        "severity": cfg.retrieval_weights.severity,
    }


def test_sources_endpoint_returns_real_rows_via_the_facade(tmp_path):
    """Confirms the get_sources route's Phase UI-2 refactor (raw SQL -> engine.sources()) preserves the exact response shape."""
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO sources(title, path, source_type, language, weight) VALUES ('A Source', '/p', 'book', 'en', 1.0)"
        )
        conn.commit()

    client = make_client(tmp_path)
    body = client.get("/sources", headers=auth_headers()).json()

    assert body["sources"][0]["title"] == "A Source"
    assert set(body["sources"][0]) == {"id", "title", "source_type", "manifest_id", "weight"}


def test_sources_endpoint_paginates_and_reports_total(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO sources(title, path, source_type, language, weight) VALUES (?, ?, 'book', 'en', 1.0)",
                (f"Source {i}", f"/p{i}"),
            )
        conn.commit()

    client = make_client(tmp_path)
    body = client.get("/sources?limit=2&offset=1", headers=auth_headers()).json()

    assert body["total"] == 5
    assert [s["title"] for s in body["sources"]] == ["Source 1", "Source 2"]


def test_sources_endpoint_filters_by_query(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO sources(title, path, source_type, language, weight) VALUES ('The Veiled Machine', '/p1', 'book', 'en', 1.0)"
        )
        conn.execute(
            "INSERT INTO sources(title, path, source_type, language, weight) VALUES ('Unrelated Text', '/p2', 'book', 'en', 1.0)"
        )
        conn.commit()

    client = make_client(tmp_path)
    body = client.get("/sources?q=veiled", headers=auth_headers()).json()

    assert body["total"] == 1
    assert body["sources"][0]["title"] == "The Veiled Machine"


def test_database_locked_returns_a_clean_503_not_a_raw_500(tmp_path):
    """Phase UI-6 item 4: a locked/busy sqlite database is a real, recoverable condition for this single-machine app, distinct from an actual bug."""
    client = make_client(tmp_path)
    with patch(
        "fieldhorizon.engine.FieldHorizonEngine.sources",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        response = client.get("/sources", headers=auth_headers())
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "FH_DATABASE_BUSY"
    assert "retry" in body["remediation"].lower()


def test_other_operational_errors_still_return_a_generic_500(tmp_path):
    """Only lock/busy errors get the 503 treatment -- a real SQL bug (malformed query, missing table) should not be told to 'retry in a moment'."""
    client = make_client(tmp_path)
    with patch(
        "fieldhorizon.engine.FieldHorizonEngine.sources",
        side_effect=sqlite3.OperationalError("no such table: sources"),
    ):
        response = client.get("/sources", headers=auth_headers())
    assert response.status_code == 500
    assert response.json()["code"] == "FH_INTERNAL_ERROR"


def test_lineage_not_found_returns_404(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/lineage/nonexistent_axiom_id_xyz", headers=auth_headers())
    assert response.status_code == 404


def test_provenance_not_found_returns_404_for_a_nonexistent_cycle_id(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/provenance/999999", headers=auth_headers())
    assert response.status_code == 404


def test_provenance_not_found_returns_404_for_a_nonexistent_axiom_id(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/provenance/nonexistent_axiom_id_xyz", headers=auth_headers())
    assert response.status_code == 404


def test_council_detail_response_matches_contract_shape(tmp_path):
    client = make_client(tmp_path)
    body = client.get("/councils/999", headers=auth_headers()).json()
    assert body["council_id"] == 999
    assert body["rehabilitated"] == []
    assert body["retired"] == []
    assert body["promoted"] == []


def test_weather_response_includes_canon_history_and_by_school(tmp_path):
    client = make_client(tmp_path)
    body = client.get("/weather", headers=auth_headers()).json()
    assert set(body) >= {"canon", "surface", "surface_history", "canon_history", "by_school"}
    assert body["by_school"] == []


def test_provenance_response_matches_contract_shape(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'a fragment')"
        )
        conn.commit()

    client = TestClient(create_app(cfg, TOKEN))
    body = client.get("/provenance/1", headers=auth_headers()).json()
    assert body["schema_version"] == 1
    assert body["object_type"] == "cycle"
    assert body["object_id"] == "1"
    assert set(body) >= {
        "supporting_evidence_count", "opposing_evidence_count", "circular_ancestry",
        "synthetic_dependency_ratio", "completeness_score", "missing_links",
        "weakest_link", "model_registry_ids",
    }
    assert body["opposing_evidence_count"] == 0


def test_fingerprint_not_found_returns_404(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/fingerprint/999", headers=auth_headers())
    assert response.status_code == 404


def test_export_endpoints_return_schema_versioned_payloads(tmp_path):
    client = make_client(tmp_path)
    for path in ("/export/factions", "/export/doctrines", "/export/contradictions", "/export/weather", "/export/beliefs"):
        body = client.get(path, headers=auth_headers()).json()
        assert body["schema_version"] == 1


def test_retrieve_endpoint_returns_candidates(tmp_path):
    client = make_client(tmp_path)
    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no model")):
        response = client.post("/retrieve", json={"query": "anything", "limit": 5}, headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["candidates"] == []
    # plan=False (the default) stays byte-identical to the v2-only shape for the fields that existed before item 3 --
    # the new planner-only fields are just empty/default, not populated with anything fabricated.
    assert body["used_planner"] is False
    assert body["canon_candidates"] == []
    assert body["excluded_candidates"] == []
    assert body["constraints_applied"] == []


def test_evaluation_preview_endpoint_returns_the_real_score_breakdown(tmp_path):
    client = make_client(tmp_path)
    with patch("fieldhorizon.doctrinal.embed_text", side_effect=RuntimeError("no embedding model")):
        response = client.post(
            "/evaluation/preview", json={"text": "Too short to ever pass any gate."}, headers=auth_headers()
        )
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "symbolic_density", "doctrinal_enforcement", "length_score", "structure_score",
        "stuffing_penalty", "generic_penalty", "final_score", "verdict", "notes",
    }
    assert body["verdict"] in {"CANON", "USEFUL_FRAGMENT", "HERESY", "NOISE"}
    assert body["notes"]  # a fragment this short/thin should trip at least one honest note


def test_retrieve_endpoint_with_plan_true_returns_the_extended_shape(tmp_path):
    """Phase UI-4 item 3: plan=true dispatches to the v3 planner path (diversity/exclusion reasons only exist there)."""
    client = make_client(tmp_path)
    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no model")):
        response = client.post(
            "/retrieve", json={"query": "a plain query", "limit": 5, "plan": True}, headers=auth_headers()
        )
    assert response.status_code == 200
    body = response.json()
    assert body["used_planner"] is True
    assert set(body["constraints_applied"]) == {"max_per_source", "min_direct_evidence_count", "max_synthetic_evidence_ratio"}
    assert body["candidates"] == []
    assert body["canon_candidates"] == []


def test_planner_preview_endpoint_returns_the_real_strategy_classification(tmp_path):
    client = make_client(tmp_path)
    response = client.post("/planner/preview", json={"query": "tawhid versus multiplicity"}, headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "tawhid versus multiplicity"
    assert "CONTRADICTION" in body["strategies_selected"]
    names = {s["name"] for s in body["strategies_available"]}
    assert names == {
        "LEXICAL", "VECTOR", "DOMAIN_ROUTING", "ENTITY", "MOTIF",
        "CONTRADICTION", "CANON_GENEALOGY", "SCHOOL", "TEMPORAL",
    }
    assert all(s["backed"] for s in body["strategies_available"])


def test_planner_preview_endpoint_accepts_an_as_of_timestamp(tmp_path):
    client = make_client(tmp_path)
    response = client.post(
        "/planner/preview", json={"query": "a plain query", "as_of": "2020-01-01 00:00:00"}, headers=auth_headers()
    )
    assert response.status_code == 200
    assert "TEMPORAL" in response.json()["strategies_selected"]


def test_retrieve_endpoint_accepts_a_weights_override(tmp_path):
    client = make_client(tmp_path)
    with patch("fieldhorizon.retrieval.embed_text", side_effect=RuntimeError("no model")):
        response = client.post(
            "/retrieve",
            json={
                "query": "a plain query",
                "limit": 5,
                "weights": {
                    "vector_similarity": 0.5, "bm25": 0.2, "domain_prior": 0.1, "source_weight": 0.1, "severity": 0.1,
                },
            },
            headers=auth_headers(),
        )
    assert response.status_code == 200
    assert response.json()["candidates"] == []


def test_multi_cycle_endpoint_returns_the_correlation_id_and_is_watchable_via_events(tmp_path):
    """Phase UI-4 item 4: LIVE CYCLE watches GET /events/stream?run_id=<correlation_id> during the (synchronous) launch."""
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    client = TestClient(create_app(cfg, TOKEN))

    response = client.post(
        "/multi-cycle",
        json={"query": "a plain query", "dry_run": True, "correlation_id": "test-multi-corr"},
        headers=auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["correlation_id"] == "test-multi-corr"
    assert isinstance(body["cycle_id"], int)

    events_response = client.get("/events", params={"run_id": "test-multi-corr"}, headers=auth_headers())
    event_types = [e["event_type"] for e in events_response.json()["events"]]
    assert "CycleStarted" in event_types
    assert "CycleCompleted" in event_types


def test_multi_cycle_endpoint_mints_a_correlation_id_when_none_supplied(tmp_path):
    client = make_client(tmp_path)
    body = client.post("/multi-cycle", json={"query": "q", "dry_run": True}, headers=auth_headers()).json()
    assert body["correlation_id"]


def test_multi_cycle_endpoint_rejects_a_second_concurrent_call(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    from fieldhorizon.server import create_app

    app = create_app(cfg, TOKEN)
    client = TestClient(app)

    release = threading.Event()

    def slow_multi_cycle(*args, **kwargs):
        release.wait(timeout=2)
        return 1

    results = {}

    def call(name):
        response = client.post("/multi-cycle", json={"query": "q", "dry_run": True}, headers=auth_headers())
        results[name] = response.status_code

    with patch("fieldhorizon.server.FieldHorizonEngine.multi_cycle", side_effect=slow_multi_cycle):
        t1 = threading.Thread(target=call, args=("first",))
        t1.start()
        time.sleep(0.1)
        t2 = threading.Thread(target=call, args=("second",))
        t2.start()
        time.sleep(0.1)
        release.set()
        t1.join(timeout=3)
        t2.join(timeout=3)

    assert results["first"] == 200
    assert results["second"] == 429


def test_run_schools_endpoint_mints_a_correlation_id_when_none_supplied(tmp_path):
    client = make_client(tmp_path)
    body = client.post("/schools/run", json={}, headers=auth_headers()).json()
    assert body["correlation_id"]
    assert body["schools"] == []  # below MIN_CYCLES_TO_CLUSTER in a bare fixture


def test_run_schools_endpoint_returns_the_correlation_id_supplied(tmp_path):
    client = make_client(tmp_path)
    body = client.post(
        "/schools/run", json={"correlation_id": "test-schools-corr"}, headers=auth_headers()
    ).json()
    assert body["correlation_id"] == "test-schools-corr"


def test_run_schools_endpoint_rejects_a_second_concurrent_call(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    from fieldhorizon.server import create_app

    app = create_app(cfg, TOKEN)
    client = TestClient(app)

    release = threading.Event()

    def slow_run_schools(*args, **kwargs):
        release.wait(timeout=2)
        return []

    results = {}

    def call(name):
        response = client.post("/schools/run", json={}, headers=auth_headers())
        results[name] = response.status_code

    with patch("fieldhorizon.server.FieldHorizonEngine.run_schools_clustering", side_effect=slow_run_schools):
        t1 = threading.Thread(target=call, args=("first",))
        t1.start()
        time.sleep(0.1)
        t2 = threading.Thread(target=call, args=("second",))
        t2.start()
        time.sleep(0.1)
        release.set()
        t1.join(timeout=3)
        t2.join(timeout=3)

    assert results["first"] == 200
    assert results["second"] == 429


def test_cycle_endpoint_rejects_a_second_concurrent_call(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    from fieldhorizon.server import create_app

    app = create_app(cfg, TOKEN)
    client = TestClient(app)

    release = threading.Event()

    def slow_cycle(*args, **kwargs):
        release.wait(timeout=2)
        return 1

    results = {}

    def call(name):
        response = client.post("/cycle", json={"query": "q", "dry_run": True}, headers=auth_headers())
        results[name] = response.status_code

    with patch("fieldhorizon.server.FieldHorizonEngine.cycle", side_effect=slow_cycle):
        t1 = threading.Thread(target=call, args=("first",))
        t1.start()
        time.sleep(0.1)  # let the first request acquire the lock
        t2 = threading.Thread(target=call, args=("second",))
        t2.start()
        time.sleep(0.1)
        release.set()
        t1.join(timeout=3)
        t2.join(timeout=3)

    assert results["first"] == 200
    assert results["second"] == 429


def test_ingest_manifest_endpoint_returns_the_correlation_id_and_real_entries(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    (cfg.root / "data" / "books").mkdir(parents=True, exist_ok=True)
    (cfg.root / "data" / "books" / "a.txt").write_text("Sample text.", encoding="utf-8")
    (cfg.root / "data" / "sources.yaml").write_text(
        "sources:\n  - id: a\n    title: A\n    path: data/books/a.txt\n", encoding="utf-8"
    )

    client = TestClient(create_app(cfg, TOKEN))
    response = client.post(
        "/ingest/manifest", json={"correlation_id": "test-corr-id"}, headers=auth_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["correlation_id"] == "test-corr-id"
    assert body["entries"] == [{"manifest_id": "a", "title": "A", "status": "ingested", "chunk_count": 1, "source_id": 1}]

    events_response = client.get("/events", params={"run_id": "test-corr-id"}, headers=auth_headers())
    event_types = [e["event_type"] for e in events_response.json()["events"]]
    assert event_types == ["IngestStarted", "IngestSourceCompleted", "IngestCompleted"]


def test_ingest_manifest_mints_a_correlation_id_when_none_supplied(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    (cfg.root / "data").mkdir(parents=True, exist_ok=True)
    (cfg.root / "data" / "sources.yaml").write_text("sources: []\n", encoding="utf-8")

    client = TestClient(create_app(cfg, TOKEN))
    body = client.post("/ingest/manifest", json={}, headers=auth_headers()).json()
    assert body["correlation_id"]
    assert body["entries"] == []


def test_ingest_manifest_endpoint_rejects_a_second_concurrent_call(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    from fieldhorizon.server import create_app

    app = create_app(cfg, TOKEN)
    client = TestClient(app)

    release = threading.Event()

    def slow_ingest(*args, **kwargs):
        release.wait(timeout=2)
        return []

    results = {}

    def call(name):
        response = client.post("/ingest/manifest", json={}, headers=auth_headers())
        results[name] = response.status_code

    with patch("fieldhorizon.server.FieldHorizonEngine.ingest_manifest", side_effect=slow_ingest):
        t1 = threading.Thread(target=call, args=("first",))
        t1.start()
        time.sleep(0.1)
        t2 = threading.Thread(target=call, args=("second",))
        t2.start()
        time.sleep(0.1)
        release.set()
        t1.join(timeout=3)
        t2.join(timeout=3)

    assert results["first"] == 200
    assert results["second"] == 429


def test_manifest_endpoint_returns_the_real_parsed_manifest(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    (cfg.root / "data").mkdir(parents=True, exist_ok=True)
    (cfg.root / "data" / "sources.yaml").write_text(
        "sources:\n"
        "  - id: sample\n    title: Sample\n    path: data/books/sample.txt\n"
        "    author: Someone\n    year: 2020\n    weight: 1.5\n",
        encoding="utf-8",
    )

    client = TestClient(create_app(cfg, TOKEN))
    body = client.get("/manifest", headers=auth_headers()).json()
    assert body["schema_version"] == 1
    assert body["entries"] == [
        {
            "id": "sample", "title": "Sample", "path": "data/books/sample.txt", "author": "Someone",
            "year": 2020, "language": "unknown", "license": "unknown", "domain_hints": [], "weight": 1.5, "notes": "",
        }
    ]


def test_manifest_endpoint_404s_when_no_manifest_file_exists(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    client = TestClient(create_app(cfg, TOKEN))
    response = client.get("/manifest", headers=auth_headers())
    assert response.status_code == 404
    assert response.json()["code"] == "FH_MANIFEST_NOT_FOUND"


def test_run_server_binds_to_loopback_only(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with patch("uvicorn.run") as mock_run:
        from fieldhorizon.server import run_server

        run_server(cfg, port=9999, token_file=tmp_path / "token")

    assert mock_run.call_args.kwargs["host"] == "127.0.0.1"
    assert mock_run.call_args.kwargs["port"] == 9999


def test_host_constant_is_loopback():
    assert HOST == "127.0.0.1"


def test_load_or_create_token_writes_a_new_token_with_restrictive_permissions(tmp_path):
    token_file = tmp_path / ".fh_token"
    token = load_or_create_token(token_file)

    assert token_file.exists()
    assert token_file.read_text(encoding="utf-8").strip() == token
    mode = token_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_load_or_create_token_reuses_an_existing_token(tmp_path):
    token_file = tmp_path / ".fh_token"
    first = load_or_create_token(token_file)
    second = load_or_create_token(token_file)
    assert first == second


def test_cors_middleware_allows_only_localhost_origins(tmp_path):
    client = make_client(tmp_path)
    response = client.get(
        "/status",
        headers={**auth_headers(), "Origin": "http://localhost:4000"},
    )
    assert response.headers.get("access-control-allow-origin") == "http://localhost:4000"

    response = client.get(
        "/status",
        headers={**auth_headers(), "Origin": "http://evil.example.com"},
    )
    assert "access-control-allow-origin" not in response.headers


def test_metrics_endpoint_tracks_request_count(tmp_path):
    client = make_client(tmp_path)
    client.get("/status", headers=auth_headers())
    client.get("/status", headers=auth_headers())
    body = client.get("/metrics", headers=auth_headers()).json()
    assert body["server_requests_total"] >= 2


def test_error_response_matches_structured_error_model(tmp_path):
    """FABLE Sec.13: every error is {schema_version, code, title, detail, remediation, correlation_id} -- never a raw traceback."""
    client = make_client(tmp_path)
    response = client.get("/fingerprint/999", headers=auth_headers())
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"schema_version", "code", "title", "detail", "remediation", "correlation_id"}
    assert body["code"] == "FH_SOURCE_NOT_FINGERPRINTED"
    assert body["correlation_id"]


def test_unknown_route_error_still_matches_structured_error_model(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/status", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401
    body = response.json()
    assert set(body) == {"schema_version", "code", "title", "detail", "remediation", "correlation_id"}
    assert body["code"] == "FH_HTTP_401"


def test_capabilities_endpoint_reflects_real_computed_state(tmp_path):
    client = make_client(tmp_path)
    body = client.get("/capabilities", headers=auth_headers()).json()
    assert body["schema_version"] == 1
    assert body["capabilities"]["planner_modes"]["status"] == "absent"
    assert body["capabilities"]["contradictions"]["status"] == "partial"
    assert body["capabilities"]["retrieval_planner"]["status"] == "present"
    assert "detail" in body["capabilities"]["retrieval_planner"]


def test_capabilities_rust_pressure_core_toggles_with_real_binary_presence(tmp_path):
    """Confirms rust_pressure_core is introspected live, not a hand-maintained constant (FABLE Sec.3.2's 'no fake facade' guarantee)."""
    client = make_client(tmp_path)

    with patch("fieldhorizon.diagnostics.rust_binary_path") as mock_path:
        mock_path.return_value.exists.return_value = True
        assert client.get("/capabilities", headers=auth_headers()).json()["capabilities"]["rust_pressure_core"]["status"] == "present"

        mock_path.return_value.exists.return_value = False
        assert client.get("/capabilities", headers=auth_headers()).json()["capabilities"]["rust_pressure_core"]["status"] == "absent"


def _tags_response(*model_names: str):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"models": [{"name": name} for name in model_names]}
    return response


def test_health_endpoint_is_ok_by_default(tmp_path):
    client = make_client(tmp_path)
    # Isolated from whatever's actually pulled on the machine running this
    # suite -- not the real local Ollama, so this stays deterministic in CI.
    with patch("fieldhorizon.diagnostics.requests.get", return_value=_tags_response("hermes3:8b")):
        body = client.get("/health", headers=auth_headers()).json()
    assert body["schema_version"] == 1
    assert body["status"] == "ok"
    names = {c["name"] for c in body["checks"]}
    assert names == {"db", "schema", "model_provider", "event_channel"}


def test_health_degrades_when_model_provider_is_unreachable(tmp_path):
    client = make_client(tmp_path)
    with patch("fieldhorizon.diagnostics.requests.get", side_effect=ConnectionError("refused")):
        body = client.get("/health", headers=auth_headers()).json()
    assert body["status"] == "degraded"
    provider_check = next(c for c in body["checks"] if c["name"] == "model_provider")
    assert provider_check["status"] == "degraded"
    assert "refused" in provider_check["detail"]


def test_health_degrades_when_configured_model_is_not_pulled(tmp_path):
    """Ollama process up and reachable, but the configured default_model was never `ollama pull`-ed -- a real, easy setup mistake distinct from 'Ollama unreachable'."""
    client = make_client(tmp_path)
    with patch("fieldhorizon.diagnostics.requests.get", return_value=_tags_response("some-other-model:1b")):
        body = client.get("/health", headers=auth_headers()).json()
    assert body["status"] == "degraded"
    provider_check = next(c for c in body["checks"] if c["name"] == "model_provider")
    assert provider_check["status"] == "degraded"
    assert "hermes3:8b" in provider_check["detail"]
    assert "not pulled" in provider_check["detail"]


def test_health_reports_failed_overall_when_a_check_fails(tmp_path):
    client = make_client(tmp_path)
    with patch("fieldhorizon.diagnostics._check_db", return_value=HealthCheck(name="db", status="failed", detail="boom")):
        body = client.get("/health", headers=auth_headers()).json()
    assert body["status"] == "failed"


def test_source_detail_endpoint_returns_the_real_row(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO sources(title, path, source_type, language, weight) VALUES ('A Source', '/p', 'book', 'en', 1.0)"
        )
        conn.execute(
            "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
            "VALUES (1, 0, 'ref', 'content', 1)"
        )
        conn.commit()

    client = TestClient(create_app(cfg, TOKEN))
    body = client.get("/sources/1", headers=auth_headers()).json()
    assert body["source"]["title"] == "A Source"
    assert body["source"]["chunk_count"] == 1

    chunks_body = client.get("/sources/1/chunks", headers=auth_headers()).json()
    assert len(chunks_body["chunks"]) == 1
    assert chunks_body["chunks"][0]["content"] == "content"


def test_source_detail_not_found_returns_404(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/sources/999", headers=auth_headers())
    assert response.status_code == 404
    assert response.json()["code"] == "FH_SOURCE_NOT_FOUND"


def test_cycle_detail_and_evidence_endpoints_return_the_real_stored_row(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment, parent_cycle_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("a query", "hermes3:8b", "prompt text", "response text", 0, "CANON", 0.87, "the resulting fragment", "[1]"),
        )
        cycle_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO cycle_sources(cycle_id, source_kind, ref, content) VALUES (?, 'book', 'ref-1', 'verbatim evidence')",
            (cycle_id,),
        )
        conn.commit()

    client = TestClient(create_app(cfg, TOKEN))
    body = client.get(f"/cycles/{cycle_id}", headers=auth_headers()).json()
    assert body["cycle"]["query"] == "a query"
    assert body["cycle"]["model"] == "hermes3:8b"
    assert body["cycle"]["verdict"] == "CANON"
    assert body["cycle"]["parent_cycle_ids"] == [1]

    evidence_body = client.get(f"/cycles/{cycle_id}/evidence", headers=auth_headers()).json()
    assert evidence_body["evidence"] == [{"source_kind": "book", "ref": "ref-1", "content": "verbatim evidence"}]


def test_cycle_detail_not_found_returns_404(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/cycles/999", headers=auth_headers())
    assert response.status_code == 404
    assert response.json()["code"] == "FH_CYCLE_NOT_FOUND"


def test_cycle_evidence_returns_empty_list_for_unknown_cycle(tmp_path):
    client = make_client(tmp_path)
    body = client.get("/cycles/999/evidence", headers=auth_headers()).json()
    assert body["evidence"] == []


def test_config_endpoint_returns_the_real_config_yaml(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    cfg.root.mkdir(parents=True, exist_ok=True)
    (cfg.root / "config.yaml").write_text("field_horizon:\n  tone: dark\n", encoding="utf-8")

    client = TestClient(create_app(cfg, TOKEN))
    body = client.get("/config", headers=auth_headers()).json()
    assert body["config"] == {"field_horizon": {"tone": "dark"}}


def test_registry_endpoints_round_trip_a_real_entry(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO model_registry(model_id, name, digest, active, first_seen_at) "
            "VALUES ('m1', 'hermes3:8b', 'sha256:abc', 1, '2026-01-01T00:00:00')"
        )
        conn.commit()

    client = TestClient(create_app(cfg, TOKEN))
    list_body = client.get("/registry/model", headers=auth_headers()).json()
    assert list_body["entries"][0]["id"] == "m1"

    entry_body = client.get("/registry/model/m1", headers=auth_headers()).json()
    assert entry_body["entry"]["name"] == "hermes3:8b"

    response = client.get("/registry/model/nonexistent", headers=auth_headers())
    assert response.status_code == 404
    assert response.json()["code"] == "FH_REGISTRY_ENTRY_NOT_FOUND"


def test_events_endpoint_round_trips_a_real_event(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    from fieldhorizon.events import EventRepository

    event = EventRepository(cfg).append(
        event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id="corr-1", aggregate_id=1
    )

    client = TestClient(create_app(cfg, TOKEN))
    list_body = client.get("/events", headers=auth_headers()).json()
    assert list_body["events"][0]["event_type"] == "CycleStarted"

    filtered_body = client.get("/events", params={"cycle_id": 1}, headers=auth_headers()).json()
    assert len(filtered_body["events"]) == 1

    entry_body = client.get(f"/events/{event.event_id}", headers=auth_headers()).json()
    assert entry_body["event_type"] == "CycleStarted"

    response = client.get("/events/not-a-real-id", headers=auth_headers())
    assert response.status_code == 404
    assert response.json()["code"] == "FH_EVENT_NOT_FOUND"


def test_proposal_detail_not_found_returns_404(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/proposals/999", headers=auth_headers())
    assert response.status_code == 404
    assert response.json()["code"] == "FH_PROPOSAL_NOT_FOUND"


def test_canon_as_of_cycle_and_timestamp_params(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'a fragment')"
        )
        conn.commit()

    client = TestClient(create_app(cfg, TOKEN))
    response = client.get("/canon", params={"as_of_timestamp": "2099-01-01T00:00:00"}, headers=auth_headers())
    assert response.status_code == 200

    response = client.get("/canon", params={"as_of_cycle": 999999}, headers=auth_headers())
    assert response.status_code == 404
    assert response.json()["code"] == "FH_CYCLE_NOT_FOUND"


def test_canon_empty_verdict_returns_cycles_of_every_verdict(tmp_path):
    """
    Phase UI-4 item 1 (COMMAND dashboard "recent cycles"): verdict="" is not
    a hack -- engine.canon()'s WHERE clause only filters `AND verdict = ?`
    when verdict is truthy, so passing an empty string through the real HTTP
    query string (not just calling the Python function directly) must return
    cycles of every verdict, not just the "CANON" default.
    """
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q1', 'm', 'p', 'r', 0, 'CANON', 0.9, 'f1')"
        )
        conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q2', 'm', 'p', 'r', 0, 'HERESY', 0.2, 'f2')"
        )
        conn.commit()

    client = TestClient(create_app(cfg, TOKEN))
    response = client.get("/canon", params={"verdict": "", "include_retired": "true", "limit": 10}, headers=auth_headers())
    assert response.status_code == 200
    verdicts = {entry["verdict"] for entry in response.json()["entries"]}
    assert verdicts == {"CANON", "HERESY"}


def test_why_changed_endpoint_returns_empty_transitions_for_a_cycle_with_no_temporal_history(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment) "
            "VALUES ('q', 'm', 'p', 'r', 0, 'CANON', 0.9, 'a fragment')"
        )
        conn.commit()

    client = TestClient(create_app(cfg, TOKEN))
    body = client.get("/why-changed/1", headers=auth_headers()).json()
    assert body["cycle_id"] == 1
    assert body["transitions"] == []


def _parse_sse_frames(body: str) -> list[dict]:
    frames = []
    for line in body.splitlines():
        if line.startswith("data: "):
            frames.append(json.loads(line[len("data: "):]))
    return frames


def test_events_stream_requires_auth(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/events/stream")
    assert response.status_code == 401


def test_events_stream_yields_existing_events_as_sse_frames(tmp_path):
    """
    The generator loops forever in production, ending only when the client
    disconnects (checked after each poll flushes whatever's available) -- there's
    no real socket in this in-process test transport to disconnect, so
    is_disconnected is mocked to end the loop after exactly one poll instead.
    """
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    from fieldhorizon.events import EventRepository

    repo = EventRepository(cfg)
    repo.append(event_type="CycleStarted", actor="cli", aggregate_type="cycle", correlation_id="run-1", aggregate_id=1)
    repo.append(event_type="CycleCompleted", actor="cli", aggregate_type="cycle", correlation_id="run-1", aggregate_id=1)

    client = TestClient(create_app(cfg, TOKEN))
    with patch("starlette.requests.Request.is_disconnected", return_value=True):
        response = client.get("/events/stream", headers=auth_headers())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse_frames(response.text)
    assert [f["event_type"] for f in frames] == ["CycleStarted", "CycleCompleted"]
    assert frames[0]["run_id"] == "run-1"
    assert frames[0]["cycle_id"] == "1"  # aggregate_id is stored as text throughout the events fabric
    assert frames[0]["stage"] is None
    assert frames[0]["severity"] is None
    assert "id: " in response.text


def test_events_stream_since_id_skips_earlier_events(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    from fieldhorizon.events import EventRepository

    repo = EventRepository(cfg)
    repo.append(event_type="A", actor="cli", aggregate_type="cycle", correlation_id="c")
    repo.append(event_type="B", actor="cli", aggregate_type="cycle", correlation_id="c")

    client = TestClient(create_app(cfg, TOKEN))
    with patch("starlette.requests.Request.is_disconnected", return_value=True):
        first_batch_text = client.get("/events/stream", headers=auth_headers()).text
    first_domain_id_line = next(line for line in first_batch_text.splitlines() if line.startswith("id: "))
    since_id = int(first_domain_id_line[len("id: "):])

    with patch("starlette.requests.Request.is_disconnected", return_value=True):
        response = client.get("/events/stream", params={"since_id": since_id}, headers=auth_headers())

    frames = _parse_sse_frames(response.text)
    assert [f["event_type"] for f in frames] == ["B"]


def test_events_stream_filters_by_cycle_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    from fieldhorizon.events import EventRepository

    repo = EventRepository(cfg)
    repo.append(event_type="A", actor="cli", aggregate_type="cycle", correlation_id="c", aggregate_id=1)
    repo.append(event_type="B", actor="cli", aggregate_type="cycle", correlation_id="c", aggregate_id=2)

    client = TestClient(create_app(cfg, TOKEN))
    with patch("starlette.requests.Request.is_disconnected", return_value=True):
        response = client.get("/events/stream", params={"cycle_id": 2}, headers=auth_headers())

    frames = _parse_sse_frames(response.text)
    assert [f["event_type"] for f in frames] == ["B"]


def test_ui_is_not_mounted_when_no_frontend_has_been_built(tmp_path):
    """State before `scripts/gui-build` has ever run: apps/desktop/dist doesn't exist."""
    with patch("fieldhorizon.server.UI_DIST_DIR", tmp_path / "no_such_dist"):
        client = make_client(tmp_path)
        response = client.get("/ui/")
    assert response.status_code == 404


def test_ui_serves_the_built_frontend_when_present(tmp_path):
    dist_dir = tmp_path / "fake_dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html><body>field horizon shell</body></html>", encoding="utf-8")
    (dist_dir / "assets").mkdir()
    (dist_dir / "assets" / "real.js").write_text("console.log('real asset');", encoding="utf-8")

    with patch("fieldhorizon.server.UI_DIST_DIR", dist_dir):
        client = make_client(tmp_path)
        index_response = client.get("/ui/")
        asset_response = client.get("/ui/assets/real.js")

    assert index_response.status_code == 200
    assert "field horizon shell" in index_response.text
    assert asset_response.status_code == 200
    assert "real asset" in asset_response.text


def test_ui_falls_back_to_index_html_for_client_side_routes(tmp_path):
    """A deep link like /ui/canon must serve the SPA shell (client-side router owns it), not 404."""
    dist_dir = tmp_path / "fake_dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html><body>field horizon shell</body></html>", encoding="utf-8")

    with patch("fieldhorizon.server.UI_DIST_DIR", dist_dir):
        client = make_client(tmp_path)
        response = client.get("/ui/canon")

    assert response.status_code == 200
    assert "field horizon shell" in response.text


def test_ui_mount_does_not_require_the_api_bearer_token(tmp_path):
    """Static assets carry no secrets; the SPA itself prompts for a token to call the real API (item 2's token flow)."""
    dist_dir = tmp_path / "fake_dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    with patch("fieldhorizon.server.UI_DIST_DIR", dist_dir):
        client = make_client(tmp_path)
        response = client.get("/ui/")  # deliberately no Authorization header

    assert response.status_code == 200


def test_event_ledger_writes_jsonl_for_server_requests(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    from fieldhorizon.server import create_app

    app = create_app(cfg, TOKEN)
    client = TestClient(app)
    client.get("/status", headers=auth_headers())

    events_dir = cfg.logs / "events"
    files = list(events_dir.glob("events_*.jsonl"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert '"event": "server_request"' in content
    assert '"path": "/status"' in content


def test_dream_run_not_found_returns_404(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/dreams/2020-01-01_00-00-00", headers=auth_headers())
    assert response.status_code == 404


def test_semantic_top_rejects_an_invalid_kind(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/semantics/top?kind=not_a_real_kind", headers=auth_headers())
    assert response.status_code == 400


def test_semantic_neighbors_rejects_an_invalid_kind(tmp_path):
    client = make_client(tmp_path)
    response = client.get("/semantics/neighbors?kind=not_a_real_kind&label=x", headers=auth_headers())
    assert response.status_code == 400


def test_semantic_top_returns_real_data(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        conn.execute("INSERT INTO sources(title, path, source_type) VALUES ('t', 'p', 'book')")
        conn.execute(
            "INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate) "
            "VALUES (1, 0, 'ref', 'content', 1)"
        )
        conn.execute("INSERT INTO chunk_concepts(chunk_id, domain, confidence) VALUES (1, 'tawhid', 1.0)")
        conn.commit()

    client = TestClient(create_app(cfg, TOKEN))
    body = client.get("/semantics/top?kind=domain", headers=auth_headers()).json()
    assert body["nodes"] == [{"kind": "domain", "label": "tawhid", "count": 1}]


def test_apply_proposal_endpoint_returns_404_for_an_unknown_number(tmp_path):
    client = make_client(tmp_path)
    response = client.post("/proposals/1/apply", headers=auth_headers())
    assert response.status_code == 404


def test_reject_proposal_endpoint_returns_404_for_an_unknown_number(tmp_path):
    client = make_client(tmp_path)
    response = client.post("/proposals/1/reject", headers=auth_headers())
    assert response.status_code == 404


def test_reject_proposal_endpoint_moves_a_real_proposal(tmp_path):
    from fieldhorizon.dream import write_ontology_proposal

    cfg = make_config(tmp_path)
    init_db(cfg.database)
    write_ontology_proposal(
        cfg, {"motif": "a_motif", "count": 7, "sample_chunks": [{"id": 1, "excerpt": "an excerpt"}]}
    )

    client = TestClient(create_app(cfg, TOKEN))
    response = client.post("/proposals/1/reject", headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["number"] == 1
    assert (cfg.root / "proposals" / "rejected").exists()


def test_apply_proposal_endpoint_wiring_with_a_mocked_ontology_and_parity_check(tmp_path):
    """
    Mirrors test_proposals.py's own apply-success test: DEFAULT_ONTOLOGY_PATH
    is a module-level constant, not derived from cfg, so an unpatched call
    here would merge into and git-commit the *real* repo's ontology.yaml --
    exactly what FABLE Sec.10.6's "never auto-apply" is guarding against.
    This test only proves the route itself is wired to engine.proposal_apply,
    not apply_proposal's own logic (already covered in test_proposals.py).
    """
    from fieldhorizon.dream import write_ontology_proposal

    cfg = make_config(tmp_path)
    init_db(cfg.database)
    write_ontology_proposal(
        cfg, {"motif": "a_motif", "count": 7, "sample_chunks": [{"id": 1, "excerpt": "an excerpt"}]}
    )
    fake_ontology = tmp_path / "fake_ontology.yaml"
    fake_ontology.write_text("domains: {}\n", encoding="utf-8")

    with (
        patch("fieldhorizon.proposals.DEFAULT_ONTOLOGY_PATH", fake_ontology),
        patch("fieldhorizon.proposals._run_ontology_parity_check"),
        patch("fieldhorizon.proposals.subprocess.run"),
    ):
        client = TestClient(create_app(cfg, TOKEN))
        response = client.post("/proposals/1/apply", headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["number"] == 1
