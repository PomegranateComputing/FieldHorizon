"""
The /db routes: authentication, input validation, idempotent launch, operator controls, read models,
redacted evidence export, recovery, and the launcher's shutdown route -- over the in-memory target.
"""
from __future__ import annotations

import io
import json
import threading
import time
import zipfile

import pytest
from dbeng_fakes import FakeAdapter, FakeExecutor, FakeServer, make_config, make_target
from fastapi.testclient import TestClient

from fieldhorizon.db import init_db
from fieldhorizon.dbeng.targets import write_targets
from fieldhorizon.server import create_app

TOKEN = "t0k"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Gate:
    """Holds the fake measurement until released, so a test can act while an operation is running."""

    def __init__(self):
        self.event = threading.Event()
        self.event.set()

    def close(self):
        self.event.clear()

    def open(self):
        self.event.set()


@pytest.fixture
def env(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    target = make_target()
    write_targets(cfg.root / "dbeng_targets.yaml", {target.name: target})
    server = FakeServer()
    gate = Gate()

    class GatedAdapter(FakeAdapter):
        def explain(self, sql, params=None, *, write=False):
            gate.event.wait(timeout=30)
            return super().explain(sql, params, write=write)

    adapter = GatedAdapter(target, server)
    app = create_app(cfg, TOKEN, db_adapter_factory=lambda t: adapter,
                     db_executor_factory=lambda t, p, operation_id: FakeExecutor(t, p, operation_id=operation_id, server=server))
    return TestClient(app), adapter, gate, cfg


def wait_finished(client, operation_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/db/operations/{operation_id}/view", headers=AUTH).json()
        if body["operation"]["finished_at"]:
            return body
        time.sleep(0.05)
    raise AssertionError("operation did not finish")


def wait_status(client, operation_id, status, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get(f"/db/operations/{operation_id}/view", headers=AUTH).json()["operation"]["status"] == status:
            return
        time.sleep(0.05)
    raise AssertionError(f"operation never reached {status}")


def test_every_db_route_needs_the_token(env):
    c, *_ = env
    for path in ("/db/targets", "/db/overview", "/db/policy", "/db/operations-page"):
        assert c.get(path).status_code in (401, 403)
    assert c.post("/db/operations", json={"target": "fake"}).status_code in (401, 403)
    assert c.post("/admin/shutdown").status_code in (401, 403)


def test_targets_and_workloads_are_listed_without_secrets(env):
    c, *_ = env
    body = c.get("/db/targets?check=true", headers=AUTH).json()
    assert body["targets"][0]["name"] == "fake"
    assert body["targets"][0]["reachable"] is True and body["targets"][0]["identity_matches"] is True
    assert "passfile" not in json.dumps(c.get("/db/overview", headers=AUTH).json())
    files = [w["file"] for w in c.get("/db/workloads", headers=AUTH).json()["workloads"]]
    assert {"bench_orders.yaml", "bench_orders_write_heavy.yaml", "bench_orders_mixed.yaml"} <= set(files)
    settings = c.get("/db/settings", headers=AUTH).json()
    assert settings["passfile_configured"] == {"fake": False} and "pause" in settings["controls"]


@pytest.mark.parametrize("payload, status", [
    ({"target": "nope"}, 404),
    ({"target": "fake", "workload": "../../config.yaml"}, 400),
    ({"target": "fake", "workload": "missing.yaml"}, 400),
    ({"target": "fake", "request_id": "bad id; DROP TABLE"}, 422),
    ({"target": "fake", "sql": "DROP TABLE bench.orders"}, 202),  # unknown fields are ignored: there is no SQL input at all
])
def test_bad_operation_requests_are_refused(env, payload, status):
    c, *_ = env
    response = c.post("/db/operations", json=payload, headers=AUTH)
    assert response.status_code == status
    if status == 202:
        wait_finished(c, response.json()["operation_id"])


def test_a_launch_is_idempotent_and_a_second_change_cannot_start(env):
    c, adapter, gate, _ = env
    gate.close()
    first = c.post("/db/operations", json={"target": "fake", "request_id": "req-1"}, headers=AUTH)
    assert first.status_code == 202 and first.json()["created"] is True
    op = first.json()["operation_id"]
    double_click = c.post("/db/operations", json={"target": "fake", "request_id": "req-1"}, headers=AUTH)
    assert double_click.json() == {"operation_id": op, "status": "RUNNING", "created": False}
    other_body = c.post("/db/operations", json={"target": "fake", "request_id": "req-1", "use_llm": True}, headers=AUTH)
    assert other_body.status_code == 422
    another = c.post("/db/operations", json={"target": "fake", "request_id": "req-2"}, headers=AUTH)
    assert another.status_code == 409 and another.json()["code"] == "DB_OPERATION_RUNNING"
    gate.open()
    wait_finished(c, op)
    assert c.post("/db/operations", json={"target": "fake", "request_id": "req-1"}, headers=AUTH).json()["operation_id"] == op
    total = c.get("/db/operations-page?kind=index_optimization", headers=AUTH).json()["total"]
    assert total == 1, "retries and double clicks never created a second operation"


def test_pause_resume_and_cancel_are_honoured_at_safe_points(env):
    c, adapter, gate, cfg = env
    gate.close()
    op = c.post("/db/operations", json={"target": "fake"}, headers=AUTH).json()["operation_id"]
    pause = c.post(f"/db/operations/{op}/control", json={"action": "pause"}, headers=AUTH)
    assert pause.status_code == 200 and "safe point" in pause.json()["semantics"]
    gate.open()
    wait_status(c, op, "PAUSED")
    view = c.get(f"/db/operations/{op}/view", headers=AUTH).json()
    assert view["controls_available"] == ["resume", "cancel"]
    assert c.post(f"/db/operations/{op}/control", json={"action": "pause"}, headers=AUTH).status_code == 409
    assert c.post(f"/db/operations/{op}/control", json={"action": "resume"}, headers=AUTH).status_code == 200
    gate.close()
    wait_status(c, op, "RUNNING")
    assert c.post(f"/db/operations/{op}/control", json={"action": "cancel"}, headers=AUTH).status_code == 200
    gate.open()
    final = wait_finished(c, op)
    assert final["operation"]["status"] == "CANCELLED" and final["operation"]["cause"] == "cancelled_by_operator"
    assert adapter.indexes == {}
    assert [x["action"] for x in final["controls"]] == ["pause", "resume", "cancel"]
    assert all(x["effect"] for x in final["controls"])
    assert c.post(f"/db/operations/{op}/control", json={"action": "cancel"}, headers=AUTH).status_code == 409
    assert c.post(f"/db/operations/{op}/control", json={"action": "explode"}, headers=AUTH).status_code == 422


def test_an_operation_runs_in_the_background_and_its_read_models_and_evidence_are_served(env):
    c, adapter, gate, _ = env
    started = c.post("/db/operations", json={"target": "fake", "workload": "bench_orders.yaml"}, headers=AUTH)
    assert started.status_code == 202
    operation_id = started.json()["operation_id"]
    view = wait_finished(c, operation_id)
    # The real bench workload's SQL is not the fake's, so the fake answers with full scans: no candidate.
    assert view["operation"]["status"] == "NO_CANDIDATE"
    assert [s["step"] for s in view["timeline"]][:3] == ["preflight", "discover", "baseline"]

    overview = c.get("/db/overview", headers=AUTH).json()
    assert overview["policy"]["version"] == 2 and overview["targets"][0]["live"]["identity_verified"] is True
    assert overview["recent"][0]["operation_id"] == operation_id
    measurements = c.get(f"/db/operations/{operation_id}/measurements", headers=AUTH).json()
    assert {q["query_id"] for q in measurements["queries"]} >= {"orders_by_customer", "control_customers_pk_range"}
    report = c.get(f"/db/operations/{operation_id}/report", headers=AUTH)
    assert report.status_code == 200 and f"# DB engineering operation {operation_id}" in report.text
    verify = c.get(f"/db/operations/{operation_id}/verify", headers=AUTH).json()
    assert verify["integrity"]["all_intact"] is True and verify["attestation"]["status"] == "none"

    evidence = zipfile.ZipFile(io.BytesIO(c.get(f"/db/operations/{operation_id}/evidence", headers=AUTH).content))
    names = evidence.namelist()
    assert {"MANIFEST.json", "ATTESTATION.txt", "report.md", "baseline.json"} <= set(names)
    baseline = evidence.read("baseline.json").decode("utf-8")
    assert "customer_id = ?" in baseline or "Filter" not in baseline, "plan literals are redacted in exports"
    manifest = json.loads(evidence.read("MANIFEST.json"))
    assert manifest["redacted"] is True and manifest["data_classification"] == "synthetic"
    raw = c.get(f"/db/operations/{operation_id}/artifacts/operation.json", headers=AUTH).json()
    assert raw["target"]["passfile"] in (None, "<local path>")


def test_unredacted_export_is_refused_for_a_non_synthetic_target(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    target = make_target(data_classification="unspecified")
    write_targets(cfg.root / "dbeng_targets.yaml", {target.name: target})
    server = FakeServer()
    adapter = FakeAdapter(target, server)
    c = TestClient(create_app(cfg, TOKEN, db_adapter_factory=lambda t: adapter,
                              db_executor_factory=lambda t, p, operation_id: FakeExecutor(t, p, operation_id=operation_id, server=server)))
    op = c.post("/db/operations", json={"target": "fake"}, headers=AUTH).json()["operation_id"]
    wait_finished(c, op)
    assert c.get(f"/db/operations/{op}/evidence?redact=false", headers=AUTH).status_code == 403
    names = zipfile.ZipFile(io.BytesIO(c.get(f"/db/operations/{op}/evidence", headers=AUTH).content)).namelist()
    assert "workload.yaml" not in names, "free text from a non-synthetic target is not exported"


def test_incidents_and_manual_recovery_routes(env):
    c, adapter, *_ = env
    detection = c.get("/db/targets/fake/incidents", headers=AUTH).json()["detection"]
    assert detection["clean"] is True
    recovered = c.post("/db/recover", json={"target": "fake"}, headers=AUTH).json()
    assert recovered["status"] == "RECOVERY_NOTHING_TO_DO" and recovered["mode"] == "manual"
    detail = c.get("/db/targets/fake", headers=AUTH).json()
    assert detail["card"]["name"] == "fake" and detail["authority"] is not None


def test_shutdown_is_refused_while_an_operation_runs_unless_forced(env):
    c, adapter, gate, _ = env
    gate.close()
    op = c.post("/db/operations", json={"target": "fake"}, headers=AUTH).json()["operation_id"]
    refused = c.post("/admin/shutdown", headers=AUTH)
    assert refused.status_code == 409 and refused.json()["code"] == "DB_OPERATIONS_RUNNING"
    forced = c.post("/admin/shutdown?force=true", headers=AUTH).json()
    assert forced["cancel_requested"] == [op]
    gate.open()
    assert wait_finished(c, op)["operation"]["status"] == "CANCELLED"


def test_an_operation_whose_process_died_is_interrupted_not_running_and_cannot_be_controlled(env):
    import socket

    from fieldhorizon.dbeng.journal import Journal

    c, adapter, gate, cfg = env
    journal = Journal(cfg)
    journal.create("OP-dead", "index_optimization", "fake", {"workload_path": "x.yaml"}, "0" * 64, None)
    # A PID that does not exist on this host: the process that ran it is gone.
    journal.set_runtime("OP-dead", holder_host=socket.gethostname(), holder_pid=2_000_000_000)
    view = c.get("/db/operations/OP-dead/view", headers=AUTH).json()
    assert view["operation"]["interrupted"] is True and view["operation"]["live"] is False
    assert view["controls_available"] == []
    assert all(op["operation_id"] != "OP-dead" for op in c.get("/db/overview", headers=AUTH).json()["running"])
    refused = c.post("/db/operations/OP-dead/control", json={"action": "cancel"}, headers=AUTH)
    assert refused.status_code == 409 and refused.json()["code"] == "DB_OPERATION_INTERRUPTED"
