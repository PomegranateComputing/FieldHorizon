"""
The reproducible PostgreSQL benchmark scenario for autonomous indexing.

`bench_setup` turns an isolated test instance (scripts/dbeng/pg-test-instance.ps1)
into a known starting point:

* roles -- fh_observer (LOGIN, read-only by role default, pg_read_all_stats,
  EXECUTE on pg_control_system() and amcheck) and fh_operator (LOGIN, member
  of fh_bench_owner, which owns the bench tables: PostgreSQL has no
  CREATE INDEX privilege separate from ownership). An event trigger limits
  fh_operator to CREATE INDEX, DROP INDEX and COMMENT at the server, in
  addition to Field Horizon's own policy;
* fieldhorizon_meta.instance_marker -- a random UUID the target file pins,
  so writes can prove they are aimed at this very instance;
* bench.customers (200k rows), bench.orders (2M rows; customer_id is a
  foreign key with NO index -- the deliberate missing index), and
  bench.audit_log (a table the policy protects). Data comes from a fixed
  setseed(), so every rebuild is identical.

Passwords are generated here, written only to the libpq password file
(whose ACL the instance script restricted to the current user), and sent to
the server as SCRAM verifiers computed client-side, never as plaintext --
so they cannot surface in pg_stat_statements or a server log.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from .targets import ENVIRONMENT_ISOLATED_TEST, TargetConfig, write_targets

BENCH_DB = "fh_bench"
OWNER_ROLE = "fh_bench_owner"
OBSERVER_ROLE = "fh_observer"
OPERATOR_ROLE = "fh_operator"

_ROLE_SQL = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fh_bench_owner') THEN CREATE ROLE fh_bench_owner NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fh_observer') THEN CREATE ROLE fh_observer LOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fh_operator') THEN CREATE ROLE fh_operator LOGIN; END IF;
END $$;
GRANT fh_bench_owner TO fh_operator;
GRANT pg_read_all_stats TO fh_observer;
ALTER ROLE fh_observer SET default_transaction_read_only = on;
"""

_META_SQL = """
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS amcheck;
CREATE SCHEMA fieldhorizon_meta;
CREATE TABLE fieldhorizon_meta.instance_marker (marker uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO fieldhorizon_meta.instance_marker(marker) VALUES (gen_random_uuid());
GRANT USAGE ON SCHEMA fieldhorizon_meta TO fh_observer, fh_operator;
GRANT SELECT ON fieldhorizon_meta.instance_marker TO fh_observer, fh_operator;
GRANT EXECUTE ON FUNCTION pg_control_system() TO fh_observer;
GRANT EXECUTE ON FUNCTION bt_index_check(regclass, boolean) TO fh_observer;
CREATE FUNCTION fieldhorizon_meta.restrict_operator_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF session_user = 'fh_operator' AND tg_tag NOT IN ('CREATE INDEX', 'DROP INDEX', 'COMMENT') THEN
    RAISE EXCEPTION 'fh_operator is limited to CREATE INDEX, DROP INDEX and COMMENT (attempted %)', tg_tag;
  END IF;
END $fn$;
CREATE EVENT TRIGGER fieldhorizon_restrict_operator ON ddl_command_start
  EXECUTE FUNCTION fieldhorizon_meta.restrict_operator_ddl();
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
CREATE TABLE bench.audit_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type text NOT NULL,
    payload text,
    created_at timestamptz NOT NULL
);
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
RESET ROLE;
ANALYZE bench.customers;
ANALYZE bench.orders;
ANALYZE bench.audit_log;
GRANT USAGE ON SCHEMA bench TO fh_observer;
GRANT SELECT ON ALL TABLES IN SCHEMA bench TO fh_observer;
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
        for role in (OBSERVER_ROLE, OPERATOR_ROLE):
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
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO fh_observer, fh_operator").format(sql.Identifier(cfg.dbname)))
        sysid = _one(conn, "SELECT system_identifier::text FROM pg_control_system()")[0]

    with _admin(cfg, cfg.dbname) as conn:
        conn.execute("SET pg_stat_statements.track_utility = off")
        conn.execute(_META_SQL)
        log(f"database {cfg.dbname}: extensions, instance marker, operator DDL guard, schema bench")
        with conn.cursor() as cur:
            cur.execute(_DATA_SQL.replace("%(customers)s", str(int(cfg.customers))).replace("%(orders)s", str(int(cfg.orders))))
        marker = _one(conn, "SELECT marker::text FROM fieldhorizon_meta.instance_marker")[0]
        counts = _one(
            conn, "SELECT (SELECT count(*) FROM bench.customers), (SELECT count(*) FROM bench.orders), (SELECT count(*) FROM bench.audit_log)"
        )
        conn.execute("SELECT pg_stat_statements_reset()")
        log(f"data: customers={counts[0]} orders={counts[1]} audit_log={counts[2]} (setseed 0.42)")

    target = TargetConfig(
        name=cfg.target_name, engine="postgresql", host=cfg.host, port=cfg.port, dbname=cfg.dbname,
        observer_user=OBSERVER_ROLE, operator_user=OPERATOR_ROLE, environment=ENVIRONMENT_ISOLATED_TEST,
        passfile=str(cfg.passfile), instance_marker=marker, system_identifier=sysid, data_directory=cfg.data_directory,
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
