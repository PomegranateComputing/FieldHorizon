"""
Acceptance, refusal, coordination and recovery paths against a REAL PostgreSQL test instance.

Skipped unless FIELDHORIZON_DBENG_TEST_PASSFILE names a libpq password file holding the admin entry
of an isolated instance (see scripts/dbeng/pg-test-instance.ps1). The module builds its own database,
fh_bench_pytest, from the reproducible bench scenario (smaller: 300k orders, the same 200k customers, so
the workloads' control query covers real rows), so it never touches the
demonstration database. Roles are cluster-wide and shared with it (same names, same guards).

Process-level tests start the real CLI in child processes (each with its own Field Horizon journal)
and stop only the processes they started.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

psycopg = pytest.importorskip("psycopg")

from psycopg.conninfo import make_conninfo  # noqa: E402

from fieldhorizon.config import AppConfig  # noqa: E402
from fieldhorizon.db import init_db  # noqa: E402
from fieldhorizon.dbeng import recovery  # noqa: E402
from fieldhorizon.dbeng.adapters.postgres import PostgresAdapter  # noqa: E402
from fieldhorizon.dbeng.bench import BenchConfig, bench_setup, bench_verify  # noqa: E402
from fieldhorizon.dbeng.executor import CreateIndex, DropOwnIndex, ExecutorRefusal, PostgresExecutor  # noqa: E402
from fieldhorizon.dbeng.journal import Journal  # noqa: E402
from fieldhorizon.dbeng.operation import IndexOptimization, OptimizeRequest  # noqa: E402
from fieldhorizon.dbeng.policy import load_policy  # noqa: E402
from fieldhorizon.dbeng.service import DbService  # noqa: E402
from fieldhorizon.dbeng.targets import load_targets, write_targets  # noqa: E402
from fieldhorizon.dbeng.workload import load_workload  # noqa: E402

PASSFILE = os.environ.get("FIELDHORIZON_DBENG_TEST_PASSFILE")
PORT = int(os.environ.get("FIELDHORIZON_DBENG_TEST_PORT", "55432"))
DATA_DIR = os.environ.get("FIELDHORIZON_DBENG_TEST_DATA_DIR")
DBNAME = "fh_bench_pytest"
REPO = Path(__file__).resolve().parent.parent
MAIN_PATH = REPO / "config" / "dbeng_workloads" / "bench_orders.yaml"
HEAVY_PATH = REPO / "config" / "dbeng_workloads" / "bench_orders_write_heavy.yaml"
MAIN = load_workload(MAIN_PATH)
WRITE_HEAVY = load_workload(HEAVY_PATH)
POLICY = load_policy()

pytestmark = pytest.mark.skipif(not PASSFILE, reason="FIELDHORIZON_DBENG_TEST_PASSFILE not set: no PostgreSQL test instance")


def _config(root: Path) -> AppConfig:
    return AppConfig(
        root=root, database=root / "fh.sqlite3", books=root / "b", json_corpus=root / "j", outputs=root / "outputs",
        logs=root / "logs", ollama_base_url="http://localhost:11434", default_model="hermes3:8b", temperature=1.0,
        top_p=0.9, repeat_penalty=1.0, num_ctx=2048, book_fragments=1, json_entries=1, chunk_chars=100,
        chunk_overlap=10, tone="t", mode="m", manifestos=root / "m", embedding_model="nomic-embed-text",
    )


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("dbeng_pg")
    cfg = _config(root)
    init_db(cfg.database)
    targets_path = root / "dbeng_targets.yaml"
    try:
        target = bench_setup(BenchConfig(
            host="127.0.0.1", port=PORT, admin_user="fh_admin", passfile=Path(PASSFILE or ""),
            data_directory=DATA_DIR or str(root), targets_path=targets_path, target_name="pytest",
            dbname=DBNAME, orders=300_000, customers=200_000, reset=True,
        ), log=lambda m: None)
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL test instance unreachable: {exc}")
    return cfg, target, targets_path


def executor_factory(env, policy=POLICY, targets_path=None, clock=time.monotonic):
    cfg, target, default_targets = env

    def make(operation_id):
        return PostgresExecutor(target, policy, operation_id=operation_id, journal_ref=str(cfg.database),
                                targets_path=targets_path or default_targets, clock=clock)
    return make


def optimize(env, workload, candidates=None, target=None, policy=POLICY, test_hooks=None, fault_after=None, fault_hook=None):
    cfg, default_target, _ = env
    target = target or default_target
    adapter = PostgresAdapter(target)
    try:
        request = OptimizeRequest(target=target.name, workload_path=workload.source, candidates=candidates or [], fault_after=fault_after)
        return IndexOptimization(cfg, target, adapter, policy, workload, request, executor_factory=executor_factory(env, policy),
                                 test_hooks=test_hooks, fault_hook=fault_hook).run()
    finally:
        adapter.close()


def catalog_indexes(env) -> dict:
    adapter = PostgresAdapter(env[1])
    try:
        return {s.name: s for s in adapter.fieldhorizon_indexes()}
    finally:
        adapter.close()


def connect_as(user: str, **kwargs):
    return psycopg.connect(make_conninfo(host="127.0.0.1", port=PORT, dbname=DBNAME, user=user, passfile=PASSFILE,
                                         application_name="pytest-direct"), autocommit=True, **kwargs)


def row_counts() -> tuple:
    with connect_as("fh_admin") as c:
        return c.execute("SELECT (SELECT count(*) FROM bench.orders), (SELECT count(*) FROM bench.customers), "
                         "(SELECT count(*) FROM bench.audit_log)").fetchone()


def retained(env) -> set[str]:
    """Names of the Field Horizon indexes the target ledger shows as retained: what must remain after any test."""
    return {r["index_name"] for r in ledger_rows(env) if r["state"] == "retained"}


def ledger_rows(env) -> list[dict]:
    adapter = PostgresAdapter(env[1])
    try:
        return adapter.ledger_rows()
    finally:
        adapter.close()


# --- the CLI in child processes (each its own journal) ----------------------------------------------
def write_cli_config(root: Path) -> Path:
    template = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8")) if (REPO / "config.yaml").exists() else {}
    template.setdefault("paths", {})
    template["paths"].update({"database": str(root / "fh.sqlite3").replace("\\", "/"), "outputs": str(root / "outputs").replace("\\", "/"),
                              "logs": str(root / "logs").replace("\\", "/")})
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(template, sort_keys=False), encoding="utf-8")
    return path


def start_cli(config: Path, targets: Path, *args: str) -> subprocess.Popen:
    creation = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    return subprocess.Popen([sys.executable, "-m", "fieldhorizon.cli", "--config", str(config), "db", *args, "--targets", str(targets)],
                            cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=creation, text=True,
                            encoding="utf-8", errors="replace")


def kill_tree(proc: subprocess.Popen) -> None:
    """Hard-kills a child process this test started, and its own children (the venv launcher's interpreter)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        proc.kill()
    proc.wait(timeout=30)


def wait_for(predicate, timeout: float, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


# --- tests -------------------------------------------------------------------------------------------
def test_01_production_targets_are_never_written(env):
    outcome = optimize(env, MAIN, target=replace(env[1], name="pytest_as_production", environment="production"))
    assert outcome.status == "BLOCKED" and outcome.cause == "authority_refused"
    assert catalog_indexes(env) == {}


def test_02_poor_and_prohibited_candidates_are_rejected(env):
    summary_only = replace(MAIN, queries=tuple(q for q in MAIN.queries if q.query_id in ("orders_status_summary", "control_customers_pk_range")))
    outcome = optimize(env, summary_only, candidates=["bench.orders(status)", "bench.audit_log(event_type)", "pg_catalog.pg_class(relname)"])
    assert outcome.status == "REJECTED"
    assert catalog_indexes(env) == {}


def test_03_the_server_guard_refuses_everything_but_the_executor_templates(env):
    before = row_counts()
    refused = {
        "arbitrary index name": 'CREATE INDEX CONCURRENTLY "other_idx" ON "bench"."orders" USING btree ("status")',
        "unique index": 'CREATE UNIQUE INDEX CONCURRENTLY "fhx_u" ON "bench"."orders" USING btree ("id")',
        "expression index": 'CREATE INDEX CONCURRENTLY "fhx_e" ON "bench"."orders" USING btree ((lower("note")))',
        "non-concurrent build": 'CREATE INDEX "fhx_nc" ON "bench"."orders" USING btree ("status")',
        "smuggled statement": 'CREATE INDEX CONCURRENTLY "fhx_s" ON "bench"."orders" USING btree ("status"); DROP TABLE bench.orders',
        "drop of a foreign index": 'DROP INDEX CONCURRENTLY IF EXISTS "bench"."orders_pkey"',
        "alter table": "ALTER TABLE bench.orders ADD COLUMN x int",
        "drop table": "DROP TABLE bench.customers",
        "truncate": "TRUNCATE bench.orders",
        "delete": "DELETE FROM bench.orders WHERE id = 1",
        "insert": "INSERT INTO bench.customers VALUES (999999, 'x', now())",
        "update": "UPDATE bench.orders SET amount = 0 WHERE id = 1",
        "grant": "GRANT SELECT ON bench.orders TO PUBLIC",
        "comment on table": "COMMENT ON TABLE bench.orders IS 'x'",
        "reindex": "REINDEX TABLE bench.orders",
        "protected table": 'CREATE INDEX CONCURRENTLY "fhx_audit" ON "bench"."audit_log" USING btree ("event_type")',
    }
    outcomes = {}
    with connect_as("fh_operator") as op:
        for name, statement in refused.items():
            op.execute("SET ROLE fh_bench_owner")
            try:
                op.execute(statement)
                outcomes[name] = "ALLOWED"
            except psycopg.Error as exc:
                outcomes[name] = type(exc).__name__
            finally:
                op.execute("RESET ROLE")
        with pytest.raises(psycopg.Error):
            op.execute('CREATE INDEX CONCURRENTLY "fhx_norole" ON "bench"."orders" USING btree ("status")')  # no SET ROLE: not owner
    with connect_as("fh_measure") as meas:
        for name, statement in {"measure truncate": "TRUNCATE bench.orders", "measure delete": "DELETE FROM bench.orders",
                                "measure index": 'CREATE INDEX "fhx_m" ON bench.orders (status)'}.items():
            try:
                meas.execute(statement)
                outcomes[name] = "ALLOWED"
            except psycopg.Error as exc:
                outcomes[name] = type(exc).__name__
    assert "ALLOWED" not in outcomes.values(), outcomes
    assert catalog_indexes(env) == {}, "no residue: refused builds left no invalid index"
    assert row_counts() == before


def test_04_the_executor_refuses_out_of_authority_requests(env, tmp_path):
    cfg, target, targets_path = env
    policy_file = tmp_path / "policy.yaml"
    shutil.copyfile(POLICY.source, policy_file)
    policy = load_policy(policy_file)
    local_targets = tmp_path / "targets.yaml"
    shutil.copyfile(targets_path, local_targets)
    now = [1000.0]
    executor = PostgresExecutor(target, policy, operation_id="OP-REFUSALS", journal_ref="pytest", targets_path=local_targets,
                                clock=lambda: now[0])
    codes = {}
    try:
        executor.acquire()
        auth = executor.authorize(eligibility={"candidate_key": "bench.orders(status)"})
        cases = {
            "malicious identifier": CreateIndex("CH-1", "bench", "orders", ("status",), 'fhx_x"; DROP TABLE bench.orders; --',
                                                candidate_key="bench.orders(status)"),
            "foreign name": CreateIndex("CH-1", "bench", "orders", ("status",), "idx_status", candidate_key="bench.orders(status)"),
            "protected table": CreateIndex("CH-1", "bench", "audit_log", ("event_type",), "fhx_audit_log_event_type",
                                           candidate_key="bench.orders(status)"),
            "unauthorized candidate": CreateIndex("CH-1", "bench", "orders", ("note",), "fhx_orders_note", candidate_key="bench.orders(note)"),
        }
        for name, cmd in cases.items():
            with pytest.raises(ExecutorRefusal) as refusal:
                executor.create_index(auth, cmd)
            codes[name] = refusal.value.code
        with pytest.raises(ExecutorRefusal) as refusal:
            executor.drop_own_index(executor.authorize(), DropOwnIndex("OP-unknown", "CH-1", "bench", "orders_pkey", "x"))
        codes["drop of a foreign index"] = refusal.value.code
        with pytest.raises(ExecutorRefusal) as refusal:
            executor.drop_own_index(executor.authorize(), DropOwnIndex("OP-unknown", "CH-1", "bench", "fhx_orders_status_t", "x"))
        codes["drop by name alone"] = refusal.value.code
        cmd = CreateIndex("CH-1", "bench", "orders", ("status",), "fhx_orders_status_t", candidate_key="bench.orders(status)")
        now[0] += policy.max_authorization_age_seconds + 1
        with pytest.raises(ExecutorRefusal) as refusal:
            executor.create_index(auth, cmd)
        codes["stale authorization"] = refusal.value.code
        auth = executor.authorize(eligibility={"candidate_key": "bench.orders(status)"})
        policy_file.write_bytes(policy_file.read_bytes() + b"\n# edited\n")
        with pytest.raises(ExecutorRefusal) as refusal:
            executor.create_index(auth, cmd)
        codes["policy changed"] = refusal.value.code
        shutil.copyfile(POLICY.source, policy_file)
        changed = load_targets(local_targets)
        changed[target.name] = replace(changed[target.name], environment="production")
        write_targets(local_targets, changed)
        with pytest.raises(ExecutorRefusal) as refusal:
            executor.create_index(auth, cmd)
        codes["target changed"] = refusal.value.code
        shutil.copyfile(targets_path, local_targets)
        with connect_as("fh_admin") as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (executor.backend_pid,))
        time.sleep(0.5)
        with pytest.raises(ExecutorRefusal) as refusal:
            executor.create_index(auth, cmd)
        codes["session lost"] = refusal.value.code
    finally:
        executor.release()
    wrong = replace(target, instance_marker="00000000-0000-0000-0000-000000000000")
    other = PostgresExecutor(wrong, policy, operation_id="OP-WRONG", journal_ref="pytest")
    try:
        other.acquire()
        with pytest.raises(ExecutorRefusal) as refusal:
            other.authorize()
        codes["identity mismatch"] = refusal.value.code
    finally:
        other.release()
    assert codes == {
        "malicious identifier": "invalid_identifier", "foreign name": "invalid_identifier", "protected table": "not_eligible",
        "unauthorized candidate": "authorization_stale", "drop of a foreign index": "invalid_identifier",
        "drop by name alone": "ownership_unproven",
        "stale authorization": "authorization_stale", "policy changed": "policy_changed", "target changed": "target_changed",
        "session lost": "lease_lost", "identity mismatch": "identity_mismatch",
    }
    assert catalog_indexes(env) == {}
    assert [r for r in ledger_rows(env) if r["state"] not in ("reverted",)] == []


def test_05_the_missing_index_is_found_built_measured_and_retained(env):
    outcome = optimize(env, MAIN)
    assert outcome.status == "RETAINED", outcome.message
    assert outcome.decision["candidate"]["key"] == "bench.orders(customer_id)"
    [state] = catalog_indexes(env).values()
    assert state.valid and state.marker["operation_id"] == outcome.operation_id
    assert [r["state"] for r in ledger_rows(env) if r["operation_id"] == outcome.operation_id] == ["retained"]
    adapter = PostgresAdapter(env[1])
    try:
        assert adapter.lease_state(POLICY.lease_scope)["lock_held"] is False, "the lease is released"
    finally:
        adapter.close()


def test_06_the_same_operation_again_is_a_no_op(env):
    assert optimize(env, MAIN).status == "NO_CANDIDATE"
    assert set(catalog_indexes(env)) == retained(env) and len(retained(env)) == 1


def test_07_a_measured_write_regression_is_compensated_and_confirmed(env):
    outcome = optimize(env, WRITE_HEAVY)
    assert outcome.status == "REVERTED", outcome.message
    failed = {c["name"] for c in outcome.decision["criteria"] if c["status"] != "pass"}
    assert "weighted_workload_improved" in failed
    assert set(catalog_indexes(env)) == retained(env)  # only the retained customer_id index
    assert [r["state"] for r in ledger_rows(env) if r["operation_id"] == outcome.operation_id] == ["reverted"]


def test_08_two_processes_with_separate_journals_are_coordinated_on_the_target(env, tmp_path):
    _, _, targets_path = env
    config_a, config_b = write_cli_config(tmp_path / "a"), write_cli_config(tmp_path / "b")
    first = start_cli(config_a, targets_path, "optimize-indexes", "--workload", str(MAIN_PATH))
    observer = PostgresAdapter(env[1])
    try:
        held = wait_for(lambda: observer.lease_state(POLICY.lease_scope)["lock_held"], timeout=60)
        assert held, "the first process never took the lease"
        second = start_cli(config_b, targets_path, "optimize-indexes", "--workload", str(MAIN_PATH), "--json")
        out_b, _ = second.communicate(timeout=300)
        out_a, _ = first.communicate(timeout=600)
    finally:
        observer.close()
        for proc in (first,):
            if proc.poll() is None:
                kill_tree(proc)
    assert second.returncode == 3, out_b  # BLOCKED
    assert '"cause": "lease_busy"' in out_b
    assert first.returncode in (0, 4), out_a  # it ran to a terminal state; the outcome itself is measured, not scripted
    assert Journal(_config(tmp_path / "a")).recent()[0].finished_at is not None


def test_09_a_kill_during_the_build_is_recovered_after_the_server_finishes(env, tmp_path):
    """The Windows case: the client is killed while CREATE INDEX CONCURRENTLY waits; the server completes it anyway."""
    cfg, target, targets_path = env
    child_config = write_cli_config(tmp_path / "child")
    blocker = connect_as("fh_measure")
    blocker.autocommit = False
    blocker.execute("INSERT INTO bench.orders (customer_id, status, amount, created_at, note) VALUES (1, 'new', 1, now(), 'blocker')")
    child = start_cli(child_config, targets_path, "optimize-indexes", "--workload", str(HEAVY_PATH))
    observer = PostgresAdapter(target)
    try:
        building = wait_for(lambda: [p for p in observer.index_build_progress()
                                     if (p.get("application_name") or "").startswith("fh-dbeng/operator/")], timeout=240)
        assert building, "the build never started"
        kill_tree(child)
        time.sleep(0.5)
        blocker.rollback()  # the build can now proceed, server-side, with nobody listening
        blocker.close()
        done = wait_for(lambda: not observer.lease_state(POLICY.lease_scope)["lock_held"], timeout=120, interval=0.2)
        assert done, "the orphaned backend never released the lease"
        [row] = [r for r in observer.ledger_rows(unfinished_only=True)]
        state = observer.index_state(row["index_schema"], row["index_name"])
    finally:
        observer.close()
    assert row["state"] == "applying"
    assert state is not None and state.valid and state.marker is None, "built after the client died, without its marker"

    report = recovery.recover(cfg, target, PostgresAdapter(target), POLICY, executor_factory(env), mode=recovery.MODE_MANUAL)
    assert report["status"] == "RECOVERY_DONE", report
    assert report["actions"][0]["proof"]["basis"].startswith("ledger shows an interrupted build")
    assert row["index_name"] not in catalog_indexes(env)
    # The killed process's own journal still shows it running; its own recovery closes it from the target ledger.
    closing = start_cli(child_config, targets_path, "recover")
    out, _ = closing.communicate(timeout=300)
    assert closing.returncode == 0, out
    killed = [r for r in Journal(_config(tmp_path / "child")).recent() if r.kind == "index_optimization"][0]
    assert killed.status == "RECOVERED_REVERTED"


def test_10_a_crash_after_the_build_is_recovered_at_start_up_and_recovery_is_idempotent(env, tmp_path):
    _, target, targets_path = env
    child_config = write_cli_config(tmp_path / "crash")
    child = start_cli(child_config, targets_path, "optimize-indexes", "--workload", str(HEAVY_PATH), "--fault-after", "apply")
    out, _ = child.communicate(timeout=600)
    assert child.returncode == 86, out  # hard exit right after the change, no cleanup
    assert len(set(catalog_indexes(env)) - retained(env)) == 1, "the crashed change is still on the target"
    child_cfg = _config(tmp_path / "crash")
    service = DbService(child_cfg, targets_path=targets_path)
    service.startup_reconcile(background=False)
    assert service.startup[target.name]["status"] == "RECOVERY_DONE", service.startup
    assert set(catalog_indexes(env)) == retained(env)
    crashed = [r for r in Journal(child_cfg).recent() if r.kind == "index_optimization"][0]
    assert crashed.status == "RECOVERED_REVERTED"
    assert Journal(child_cfg).runtime(crashed.operation_id)["recovery_mode"] == "automatic_startup"
    service.startup_reconcile(background=False)
    assert service.startup[target.name]["status"] == "RECOVERY_NOTHING_TO_DO"


def test_11_a_long_transaction_opened_after_the_precheck_times_the_build_out_and_is_recovered(env):
    cfg, target, _ = env
    holder: dict = {}

    def open_long_transaction():
        conn = connect_as("fh_measure")
        conn.autocommit = False
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute("SELECT count(*) FROM bench.orders WHERE id < 10").fetchone()  # holds a snapshot and a lock
        holder["conn"] = conn

    outcome = optimize(env, WRITE_HEAVY, test_hooks={"before_apply": open_long_transaction})
    try:
        assert outcome.cause == "ddl_lock_timeout", outcome.message
        assert outcome.status == "FAILED_NEEDS_RECOVERY", "the leftover cannot be dropped while the transaction lives"
    finally:
        holder["conn"].rollback()
        holder["conn"].close()
    report = recovery.recover(cfg, target, PostgresAdapter(target), POLICY, executor_factory(env), mode=recovery.MODE_MANUAL)
    assert report["status"] == "RECOVERY_DONE", report
    assert Journal(cfg).get(outcome.operation_id).status == "FAILED_RECOVERED"
    assert set(catalog_indexes(env)) == retained(env)


def test_12_a_statement_timeout_during_the_build_is_cleaned_up(env):
    outcome = optimize(env, WRITE_HEAVY, policy=replace(POLICY, statement_timeout_ms=1))
    assert outcome.status == "FAILED_RECOVERED", outcome.message
    assert outcome.cause == "ddl_statement_timeout"
    assert set(catalog_indexes(env)) == retained(env)


def test_13_the_lease_is_exclusive_and_dies_with_its_session(env):
    _, target, _ = env
    first = PostgresExecutor(target, POLICY, operation_id="OP-L1", journal_ref="pytest")
    second = PostgresExecutor(target, POLICY, operation_id="OP-L2", journal_ref="pytest")
    try:
        token = first.acquire().fencing_token
        with pytest.raises(ExecutorRefusal) as refusal:
            second.acquire()
        assert refusal.value.code == "lease_busy" and "OP-L1" in refusal.value.detail
        first.close()  # the session ends without releasing: the server frees the lock
        lease = None
        for _ in range(50):
            try:
                lease = second.acquire()
                break
            except ExecutorRefusal:
                time.sleep(0.2)
        assert lease is not None and lease.fencing_token == token + 1, "a new holder gets a new fencing token"
    finally:
        second.release()
        first.release()


def test_14_the_lab_is_consistent_and_no_operation_is_left_unfinished(env):
    cfg, target, _ = env
    adapter = PostgresAdapter(target)
    try:
        verified = bench_verify(adapter)
        detection = recovery.detect(adapter, Journal(cfg), target, POLICY)
    finally:
        adapter.close()
    assert verified["consistent"] and verified["invalid_indexes"] == []
    assert detection["clean"], detection["incidents"]
    assert Journal(cfg).unfinished() == []


def test_15_concurrent_launches_in_one_service_never_start_two_changes(env, tmp_path):
    _, target, targets_path = env
    service = DbService(_config(tmp_path), targets_path=targets_path)
    init_db(service.cfg.database)
    launches, errors = [], []

    def launch():
        try:
            launches.append(service.start_optimization(target=target.name, workload="bench_orders.yaml", candidates=[], use_llm=False,
                                                       llm_model=None, request_id="same-click"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=launch) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len({x.operation_id for x in launches}) == 1
    assert sum(1 for x in launches if x.created) == 1
    op = launches[0].operation_id
    assert wait_for(lambda: (Journal(service.cfg).get(op) or None) and Journal(service.cfg).get(op).finished_at, timeout=600, interval=0.5)
