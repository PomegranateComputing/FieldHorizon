"""The /db routes: authentication, input validation, a background operation end to end, report and evidence download."""
from __future__ import annotations

import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from test_dbeng_operation import FakeAdapter, make_config, make_target

from fieldhorizon.db import init_db
from fieldhorizon.dbeng.targets import write_targets
from fieldhorizon.server import create_app

TOKEN = "t0k"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    target = make_target()
    write_targets(cfg.root / "dbeng_targets.yaml", {target.name: target})
    adapter = FakeAdapter(target)
    return TestClient(create_app(cfg, TOKEN, db_adapter_factory=lambda t: adapter)), adapter


def wait_finished(client, operation_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/db/operations/{operation_id}", headers=AUTH).json()
        if body["operation"]["finished_at"]:
            return body
        time.sleep(0.05)
    raise AssertionError("operation did not finish")


def test_every_db_route_needs_the_token(client):
    c, _ = client
    assert c.get("/db/targets").status_code in (401, 403)
    assert c.post("/db/operations", json={"target": "fake"}).status_code in (401, 403)


def test_targets_and_workloads_are_listed_without_secrets(client):
    c, _ = client
    body = c.get("/db/targets?check=true", headers=AUTH).json()
    assert body["targets"][0]["name"] == "fake"
    assert body["targets"][0]["reachable"] is True and body["targets"][0]["identity_matches"] is True
    assert "passfile" not in body["targets"][0]
    files = [w["file"] for w in c.get("/db/workloads", headers=AUTH).json()["workloads"]]
    assert {"bench_orders.yaml", "bench_orders_write_heavy.yaml"} <= set(files)


@pytest.mark.parametrize("payload, status", [
    ({"target": "nope"}, 404),
    ({"target": "fake", "workload": "../../config.yaml"}, 400),
    ({"target": "fake", "workload": "missing.yaml"}, 400),
])
def test_bad_operation_requests_are_refused(client, payload, status):
    c, _ = client
    assert c.post("/db/operations", json=payload, headers=AUTH).status_code == status


def test_an_operation_runs_in_the_background_and_its_evidence_downloads(client):
    c, adapter = client
    started = c.post("/db/operations", json={"target": "fake", "workload": "bench_orders.yaml"}, headers=AUTH)
    assert started.status_code == 202
    operation_id = started.json()["operation_id"]

    detail = wait_finished(c, operation_id)
    # The real bench workload's SQL is not the fake's, so the fake answers with full scans: no candidate.
    assert detail["operation"]["status"] in ("NO_CANDIDATE", "RETAINED")
    assert [s["step"] for s in detail["steps"]][:3] == ["preflight", "discover", "baseline"]

    report = c.get(f"/db/operations/{operation_id}/report", headers=AUTH)
    assert report.status_code == 200 and f"# DB engineering operation {operation_id}" in report.text
    evidence = c.get(f"/db/operations/{operation_id}/evidence", headers=AUTH)
    names = zipfile.ZipFile(io.BytesIO(evidence.content)).namelist()
    assert "MANIFEST.json" in names and "report.md" in names and "baseline.json" in names

    listed = c.get("/db/operations", headers=AUTH).json()
    assert listed["running"] is None and listed["operations"][0]["operation_id"] == operation_id
