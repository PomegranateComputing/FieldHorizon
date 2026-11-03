"""
Acceptance and rejection paths against a REAL PostgreSQL test instance.

Skipped unless FIELDHORIZON_DBENG_TEST_PASSFILE names a libpq password
file holding the admin entry of an isolated instance (see
scripts/dbeng/pg-test-instance.ps1). The module builds its own database,
fh_bench_pytest, from the reproducible bench scenario (smaller: 300k
orders), so it never touches the demonstration database.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from fieldhorizon.config import AppConfig  # noqa: E402
from fieldhorizon.db import init_db  # noqa: E402
from fieldhorizon.dbeng.adapters.postgres import PostgresAdapter  # noqa: E402
from fieldhorizon.dbeng.bench import BenchConfig, bench_setup, bench_verify  # noqa: E402
from fieldhorizon.dbeng.journal import Journal  # noqa: E402
from fieldhorizon.dbeng.operation import IndexOptimization, OptimizeRequest, recover  # noqa: E402
from fieldhorizon.dbeng.policy import load_policy  # noqa: E402
from fieldhorizon.dbeng.workload import load_workload  # noqa: E402

PASSFILE = os.environ.get("FIELDHORIZON_DBENG_TEST_PASSFILE")
PORT = int(os.environ.get("FIELDHORIZON_DBENG_TEST_PORT", "55432"))
DATA_DIR = os.environ.get("FIELDHORIZON_DBENG_TEST_DATA_DIR")
REPO = Path(__file__).resolve().parent.parent
MAIN = load_workload(REPO / "config" / "dbeng_workloads" / "bench_orders.yaml")
WRITE_HEAVY = load_workload(REPO / "config" / "dbeng_workloads" / "bench_orders_write_heavy.yaml")
POLICY = load_policy()

pytestmark = pytest.mark.skipif(not PASSFILE, reason="FIELDHORIZON_DBENG_TEST_PASSFILE not set: no PostgreSQL test instance")


class SimulatedCrash(BaseException):
    pass


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("dbeng_pg")
    cfg = AppConfig(
        root=root, database=root / "fh.sqlite3", books=root / "b", json_corpus=root / "j", outputs=root / "outputs",
        logs=root / "logs", ollama_base_url="http://localhost:11434", default_model="hermes3:8b", temperature=1.0,
        top_p=0.9, repeat_penalty=1.0, num_ctx=2048, book_fragments=1, json_entries=1, chunk_chars=100,
        chunk_overlap=10, tone="t", mode="m", manifestos=root / "m", embedding_model="nomic-embed-text",
    )
    init_db(cfg.database)
    try:
        target = bench_setup(BenchConfig(
            host="127.0.0.1", port=PORT, admin_user="fh_admin", passfile=Path(PASSFILE or ""),
            data_directory=DATA_DIR or str(root), targets_path=root / "targets.yaml", target_name="pytest",
            dbname="fh_bench_pytest", orders=300_000, customers=50_000, reset=True,
        ), log=lambda m: None)
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL test instance unreachable: {exc}")
    return cfg, target


def optimize(env, workload, candidates=None, target=None, fault_after=None, fault_hook=None):
    cfg, default_target = env
    target = target or default_target
    adapter = PostgresAdapter(target)
    try:
        request = OptimizeRequest(target=target.name, workload_path=workload.source, candidates=candidates or [],
                                  fault_after=fault_after)
        return IndexOptimization(cfg, target, adapter, POLICY, workload, request, fault_hook=fault_hook).run()
    finally:
        adapter.close()


def catalog_indexes(env) -> dict:
    adapter = PostgresAdapter(env[1])
    try:
        return {s.name: s for s in adapter.fieldhorizon_indexes()}
    finally:
        adapter.close()


def test_1_production_targets_are_never_written(env):
    outcome = optimize(env, MAIN, target=replace(env[1], name="pytest_as_production", environment="production"))
    assert outcome.status == "BLOCKED"
    assert catalog_indexes(env) == {}


def test_2_poor_and_prohibited_candidates_are_rejected(env):
    summary_only = replace(MAIN, queries=tuple(q for q in MAIN.queries if q.query_id == "orders_status_summary"))
    outcome = optimize(env, summary_only, candidates=["bench.orders(status)", "bench.audit_log(event_type)", "pg_catalog.pg_class(relname)"])
    assert outcome.status == "REJECTED"
    assert catalog_indexes(env) == {}


def test_3_the_missing_index_is_found_built_measured_and_retained(env):
    outcome = optimize(env, MAIN)
    assert outcome.status == "RETAINED", outcome.message
    assert outcome.decision["candidate"]["key"] == "bench.orders(customer_id)"
    [state] = catalog_indexes(env).values()
    assert state.valid and state.marker["operation_id"] == outcome.operation_id


def test_4_the_same_operation_again_is_a_no_op(env):
    assert optimize(env, MAIN).status == "NO_CANDIDATE"
    assert len(catalog_indexes(env)) == 1


def test_5_a_measured_write_regression_is_rolled_back(env):
    outcome = optimize(env, WRITE_HEAVY)
    assert outcome.status == "REVERTED", outcome.message
    failed = {c["name"] for c in outcome.decision["criteria"] if c["status"] != "pass"}
    assert "weighted_workload_improved" in failed
    assert [s.table for s in catalog_indexes(env).values()] == ["orders"]  # only the retained customer_id index


def test_6_a_crash_after_the_change_is_recovered_and_the_database_stays_consistent(env):
    cfg, target = env

    def crash(step):
        raise SimulatedCrash(step)

    with pytest.raises(SimulatedCrash):
        optimize(env, WRITE_HEAVY, fault_after="apply", fault_hook=crash)
    assert len(catalog_indexes(env)) == 2  # the note index is live with no decision

    adapter = PostgresAdapter(target)
    try:
        actions = recover(cfg, target, adapter, POLICY)
        assert any(a.get("status") == "RECOVERED_REVERTED" for a in actions)
        assert recover(cfg, target, adapter, POLICY) == []
        verified = bench_verify(adapter)
    finally:
        adapter.close()
    assert len(catalog_indexes(env)) == 1
    assert verified["consistent"] and verified["invalid_indexes"] == []
    assert Journal(cfg).unfinished() == []
