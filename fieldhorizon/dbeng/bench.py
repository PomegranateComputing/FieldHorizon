"""
The reproducible PostgreSQL laboratory for autonomous indexing (DB-02).

`bench_setup` turns an isolated test instance (scripts/dbeng/pg-test-instance.ps1)
into a known starting point. Every privilege below was first verified on the
instance itself (_qualification/db02/logs/B0*_pg_mechanism_probe*):

Identities
* fh_observer  -- discovery, plans, statistics. Read-only by role default,
  pg_read_all_stats, EXECUTE on pg_control_system() and amcheck.
* fh_measure   -- workload measurement only: SELECT on the bench tables the
  workloads read, INSERT on bench.orders (write scenarios run inside
  transactions the measurement code always rolls back). No ownership.
* fh_operator  -- the privileged executor. PostgreSQL has no CREATE INDEX
  privilege apart from table ownership, so it is a member of fh_bench_owner,
  but WITH INHERIT FALSE: it holds no owner privilege until it explicitly
  `SET ROLE fh_bench_owner`, and the server confines what it can do then:
    - a read-only SECURITY DEFINER event trigger refuses, before execution,
      any DDL from fh_operator whose text is not exactly one of the executor
      templates in templates.py (CREATE/DROP INDEX CONCURRENTLY on an fhx_
      b-tree index, COMMENT ON INDEX fhx_). Read-only matters: a guard that
      writes makes DROP INDEX CONCURRENTLY fail ("must be first action in
      transaction"); auditing goes to the server log via RAISE LOG;
    - statement triggers refuse INSERT/UPDATE/DELETE/TRUNCATE from
      fh_operator on every lab table, even while it acts as the owner.
  Residual owner powers the server cannot intercept (measured): SELECT on
  lab data, LOCK TABLE, VACUUM, ANALYZE, CLUSTER -- see AUTHORITY_MODEL.md.
* fh_protected_owner -- owns bench.audit_log, the protected table. The
  operator is not a member: the protection is the server's, not only policy.

Coordination (fieldhorizon_meta, written only by fh_operator, read by the observer)
* operation_lease -- one row per scope, with a fencing token incremented at
  every acquisition; paired with a session-level advisory lock held by the
  executor for the whole operation (released by the server when that
  session ends).
* change_ledger -- every change, written BEFORE the DDL is sent, so any
  Field Horizon process -- whatever its local journal -- can find and prove
  an unfinished change after a crash.
* instance_marker -- a random UUID the target file pins.

Data: bench.customers (200k), bench.orders (2M; customer_id is a foreign key
with NO index -- the deliberate missing index), bench.audit_log (100k).
Generated from setseed(0.42): synthetic, identical at every rebuild.

Passwords are generated here, written only to the libpq password file (owner-
only ACL), and sent to the server as SCRAM verifiers computed client-side.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from .targets import ENVIRONMENT_ISOLATED_TEST, TargetConfig, write_targets
from .templates import COMMENT_INDEX_TEXT, CREATE_INDEX_TEXT, DROP_INDEX_TEXT

BENCH_DB = "fh_bench"
OWNER_ROLE = "fh_bench_owner"
PROTECTED_OWNER_ROLE = "fh_protected_owner"
OBSERVER_ROLE = "fh_observer"
MEASURE_ROLE = "fh_measure"
OPERATOR_ROLE = "fh_operator"
LEASE_SCOPE = "index_optimization"
LAB_TABLES = ("customers", "orders", "audit_log")

_ROLE_SQL = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fh_bench_owner') THEN CREATE ROLE fh_bench_owner NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fh_protected_owner') THEN CREATE ROLE fh_protected_owner NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fh_observer') THEN CREATE ROLE fh_observer LOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fh_measure') THEN CREATE ROLE fh_measure LOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fh_operator') THEN CREATE ROLE fh_operator LOGIN; END IF;
END $$;
REVOKE fh_bench_owner FROM fh_operator;
GRANT fh_bench_owner TO fh_operator WITH INHERIT FALSE, SET TRUE;
GRANT pg_read_all_stats TO fh_observer;
ALTER ROLE fh_observer SET default_transaction_read_only = on;
"""


def _guard_sql() -> str:
    # Dollar-quoted regex literals; the patterns are the exact ones the executor's statements match.
    return f"""
CREATE FUNCTION fieldhorizon_meta.guard_operator_ddl() RETURNS event_trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $fn$
DECLARE q text := btrim(current_query(), E' \\n\\t\\r;');
BEGIN
  IF session_user <> 'fh_operator' THEN RETURN; END IF;
  IF (tg_tag = 'CREATE INDEX' AND q ~ $re${CREATE_INDEX_TEXT}$re$)
     OR (tg_tag = 'DROP INDEX' AND q ~ $re${DROP_INDEX_TEXT}$re$)
     OR (tg_tag = 'COMMENT' AND q ~ $re${COMMENT_INDEX_TEXT}$re$) THEN
    RAISE LOG 'fieldhorizon guard: allowed % for fh_operator: %', tg_tag, left(q, 300);
    RETURN;
  END IF;
  RAISE LOG 'fieldhorizon guard: refused % for fh_operator: %', tg_tag, left(q, 300);
  RAISE EXCEPTION 'fieldhorizon guard: % refused for fh_operator (only the executor templates are accepted)', tg_tag
    USING ERRCODE = 'insufficient_privilege';
END $fn$;
CREATE EVENT TRIGGER fieldhorizon_guard_operator ON ddl_command_start
  EXECUTE FUNCTION fieldhorizon_meta.guard_operator_ddl();

CREATE FUNCTION fieldhorizon_meta.guard_operator_dml() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $fn$
BEGIN
  IF session_user = 'fh_operator' THEN
    RAISE EXCEPTION 'fieldhorizon guard: fh_operator may not % %.%', TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NULL;
END $fn$;
"""


_META_SQL = """
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS amcheck;
CREATE SCHEMA fieldhorizon_meta;
CREATE TABLE fieldhorizon_meta.instance_marker (marker uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO fieldhorizon_meta.instance_marker(marker) VALUES (gen_random_uuid());
CREATE TABLE fieldhorizon_meta.operation_lease (
    scope text PRIMARY KEY,
    fencing_token bigint NOT NULL DEFAULT 0,
    holder_operation_id text,
    holder_host text,
    holder_pid integer,
    holder_backend_pid integer,
    holder_journal text,
    acquired_at timestamptz,
    heartbeat_at timestamptz,
    released_at timestamptz
);
INSERT INTO fieldhorizon_meta.operation_lease(scope) VALUES ('index_optimization');
CREATE TABLE fieldhorizon_meta.change_ledger (
    operation_id text NOT NULL,
    change_id text NOT NULL,
    fencing_token bigint NOT NULL,
    action text NOT NULL CHECK (action IN ('create_index')),
    index_schema text NOT NULL,
    index_name text NOT NULL,
    table_name text NOT NULL,
    columns text[] NOT NULL,
    definition text,
    index_oid bigint,
    state text NOT NULL CHECK (state IN ('applying', 'applied', 'retained', 'reverting', 'reverted')),
    holder_host text,
    holder_journal text,
    recovered_by text,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (operation_id, change_id)
);
GRANT USAGE ON SCHEMA fieldhorizon_meta TO fh_observer, fh_operator, fh_measure;
GRANT SELECT ON fieldhorizon_meta.instance_marker TO fh_observer, fh_operator, fh_measure;
GRANT SELECT ON fieldhorizon_meta.operation_lease, fieldhorizon_meta.change_ledger TO fh_observer;
GRANT SELECT, UPDATE ON fieldhorizon_meta.operation_lease TO fh_operator;
GRANT SELECT, INSERT, UPDATE ON fieldhorizon_meta.change_ledger TO fh_operator;
GRANT EXECUTE ON FUNCTION pg_control_system() TO fh_observer, fh_operator;
GRANT EXECUTE ON FUNCTION bt_index_check(regclass, boolean) TO fh_observer;
CREATE SCHEMA bench AUTHORIZATION fh_bench_owner;
"""

_DATA_SQL = """
SET ROLE fh_bench_owner;
CREATE TABLE bench.customers (
    id integer PRIMARY KEY,
    region text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE bench.orders (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES bench.customers(id),  -- deliberately unindexed
    status text NOT NULL,
    amount numeric(12, 2) NOT NULL,
    created_at timestamptz NOT NULL,
    note text NOT NULL
);
RESET ROLE;
CREATE TABLE bench.audit_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type text NOT NULL,
    payload text,
    created_at timestamptz NOT NULL
);
ALTER TABLE bench.audit_log OWNER TO fh_protected_owner;
SELECT setseed(0.42);
INSERT INTO bench.customers
SELECT g, (ARRAY['north','south','east','west','central','coast','mountain','islands'])[1 + (g % 8)],
       timestamptz '2025-01-01 00:00:00+00' + (g % 365) * interval '1 day'
FROM generate_series(1, %(customers)s) g;
INSERT INTO bench.orders (customer_id, status, amount, created_at, note)
SELECT 1 + floor(random() * %(customers)s)::int,
       (ARRAY['new','paid','shipped','delivered','cancelled'])[1 + floor(random() * 5)::int],
       round((random() * 500)::numeric, 2),
       timestamptz '2025-01-01 00:00:00+00' + random() * interval '365 days',
       md5(g::text)
FROM generate_series(1, %(orders)s) g;
INSERT INTO bench.audit_log (event_type, payload, created_at)
SELECT (ARRAY['login','logout','update','delete','export','admin'])[1 + (g % 6)], md5(g::text),
       timestamptz '2025-01-01 00:00:00+00' + (g % 365) * interval '1 day'
FROM generate_series(1, 100000) g;
ANALYZE bench.customers;
ANALYZE bench.orders;
ANALYZE bench.audit_log;
GRANT USAGE ON SCHEMA bench TO fh_observer, fh_measure;
GRANT SELECT ON ALL TABLES IN SCHEMA bench TO fh_observer;
-- Read-only on the identity sequences, so the side effect of rolled-back measurement inserts
-- (consumed identity values) can be observed and reported.
GRANT SELECT ON ALL SEQUENCES IN SCHEMA bench TO fh_observer;
GRANT SELECT ON bench.customers, bench.orders TO fh_measure;
GRANT INSERT ON bench.orders TO fh_measure;
"""


@dataclass
class BenchConfig:
    host: str
    port: int
    admin_user: str
    passfile: Path
    data_directory: str | None
    targets_path: Path
    target_name: str = "bench_local"
    dbname: str = BENCH_DB
    orders: int = 2_000_000
    customers: int = 200_000
    reset: bool = False


def _new_secret() -> str:
    return secrets.token_urlsafe(32)  # URL-safe: no ':' or '\' to escape in the password file


def _store_password(passfile: Path, host: str, port: int, user: str, secret: str) -> None:
    prefix = f"{host}:{port}:*:{user}:"
    lines = [line for line in passfile.read_text(encoding="ascii").splitlines() if line and not line.startswith(prefix)] \
        if passfile.exists() else []
    lines.append(prefix + secret)
    # Rewriting the existing file in place keeps its owner-only ACL.
    with passfile.open("w", encoding="ascii", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _one(conn: psycopg.Connection, query: str) -> tuple:
    row = conn.execute(query).fetchone()
    if row is None:
        raise RuntimeError(f"no row from {query!r}")
    return row


def _admin(cfg: BenchConfig, dbname: str) -> psycopg.Connection:
    conninfo = make_conninfo(host=cfg.host, port=cfg.port, dbname=dbname, user=cfg.admin_user,
                             passfile=str(cfg.passfile), application_name="fieldhorizon-bench-setup", connect_timeout=10)
    return psycopg.connect(conninfo, autocommit=True)


def bench_setup(cfg: BenchConfig, log=print) -> TargetConfig:
    with _admin(cfg, "postgres") as conn:
        conn.execute("SET pg_stat_statements.track_utility = off")
        conn.execute(_ROLE_SQL)
        for role in (OBSERVER_ROLE, MEASURE_ROLE, OPERATOR_ROLE):
            secret = _new_secret()
            verifier = conn.pgconn.encrypt_password(secret.encode(), role.encode(), b"scram-sha-256").decode()
            conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(verifier)))
            _store_password(cfg.passfile, cfg.host, cfg.port, role, secret)
            log(f"role {role}: password rotated (stored in the password file only)")
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (cfg.dbname,)).fetchone()
        if exists and cfg.reset:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cfg.dbname)))
            log(f"database {cfg.dbname}: dropped (--reset)")
            exists = None
        if exists:
            raise RuntimeError(f"database {cfg.dbname} already exists; pass --reset to rebuild it")
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cfg.dbname)))
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(cfg.dbname)))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO fh_observer, fh_measure, fh_operator").format(sql.Identifier(cfg.dbname)))
        sysid = _one(conn, "SELECT system_identifier::text FROM pg_control_system()")[0]

    with _admin(cfg, cfg.dbname) as conn:
        conn.execute("SET pg_stat_statements.track_utility = off")
        conn.execute(_META_SQL)
        conn.execute(_guard_sql())
        log(f"database {cfg.dbname}: extensions, instance marker, coordination tables, operator guards, schema bench")
        with conn.cursor() as cur:
            cur.execute(_DATA_SQL.replace("%(customers)s", str(int(cfg.customers))).replace("%(orders)s", str(int(cfg.orders))))
        for table in LAB_TABLES:
            conn.execute(sql.SQL(
                "CREATE TRIGGER fieldhorizon_guard_operator_dml BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON bench.{} "
                "FOR EACH STATEMENT EXECUTE FUNCTION fieldhorizon_meta.guard_operator_dml()"
            ).format(sql.Identifier(table)))
        # A settled starting state: without this, autovacuum processes the freshly loaded tables
        # during the first measurements (observed in DB-02 run D01) and its bursts become noise.
        for table in LAB_TABLES:
            conn.execute(sql.SQL("VACUUM (ANALYZE) bench.{}").format(sql.Identifier(table)))
        log("lab settled: VACUUM (ANALYZE) on every bench table")
        marker = _one(conn, "SELECT marker::text FROM fieldhorizon_meta.instance_marker")[0]
        counts = _one(
            conn, "SELECT (SELECT count(*) FROM bench.customers), (SELECT count(*) FROM bench.orders), (SELECT count(*) FROM bench.audit_log)"
        )
        conn.execute("SELECT pg_stat_statements_reset()")
        log(f"data: customers={counts[0]} orders={counts[1]} audit_log={counts[2]} (synthetic, setseed 0.42)")

    target = TargetConfig(
        name=cfg.target_name, engine="postgresql", host=cfg.host, port=cfg.port, dbname=cfg.dbname,
        observer_user=OBSERVER_ROLE, operator_user=OPERATOR_ROLE, environment=ENVIRONMENT_ISOLATED_TEST,
        passfile=str(cfg.passfile), instance_marker=marker, system_identifier=sysid, data_directory=cfg.data_directory,
        measure_user=MEASURE_ROLE, owner_role=OWNER_ROLE, data_classification="synthetic",
    )
    write_targets(cfg.targets_path, {target.name: target})
    log(f"target file written: {cfg.targets_path}")
    return target


def bench_verify(adapter) -> dict:
    """Consistency of the bench database as the observer sees it: invalid indexes, amcheck on every b-tree."""
    conn = adapter.observer
    invalid = [r["index"] for r in conn.execute(
        "SELECT c.relname AS index FROM pg_index x JOIN pg_class c ON c.oid = x.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'bench' AND NOT x.indisvalid"
    ).fetchall()]
    checked = []
    for row in conn.execute(
        "SELECT c.oid::regclass::text AS index FROM pg_index x JOIN pg_class c ON c.oid = x.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_am am ON am.oid = c.relam "
        "WHERE n.nspname = 'bench' AND am.amname = 'btree' AND x.indisvalid ORDER BY 1"
    ).fetchall():
        conn.execute("SELECT bt_index_check(%s::regclass, true)", (row["index"],))
        checked.append(row["index"])
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM bench.customers) AS customers, (SELECT count(*) FROM bench.orders) AS orders, "
        "(SELECT count(*) FROM bench.audit_log) AS audit_log"
    ).fetchone()
    return {"invalid_indexes": invalid, "amcheck_passed": checked, "row_counts": dict(counts), "consistent": not invalid}
