"""
PostgreSQL adapter (psycopg 3): observation and workload measurement only.

Two identities here, each on its own lazily opened, autocommit connection:

* observer -- discovery, plans of record, statistics, server activity and
  the coordination tables. Read-only by role default and by session setting.
* measure  -- workload execution for measurement: EXPLAIN ANALYZE and
  client-timed plain execution. Writes run inside a transaction that is
  always rolled back. It owns nothing.

No DDL leaves this module: the privileged operator identity lives only in
executor.PostgresExecutor. Every connection is tagged with
``application_name = fh-dbeng/<identity>/<operation>`` so that, after a
client loss, server-side activity can be traced to the operation and the
identity that started it. Every identifier reaching SQL is composed with
psycopg.sql, never with string formatting. Statistics that cannot be read
are reported with an availability status, never defaulted to zero.
"""
from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from ..model import AVAILABLE, UNAVAILABLE, UNSUPPORTED, UNVERIFIED, ExplainResult, IndexState
from ..targets import TargetConfig, redact

APPLICATION_PREFIX = "fh-dbeng"
LOCK_NAMESPACE = "fieldhorizon.dbeng"
MARKER_KEY = "fieldhorizon"
MARKER_VALUE = "dbeng"

SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

RELEVANT_SETTINGS = (
    "server_version", "shared_buffers", "work_mem", "maintenance_work_mem", "effective_cache_size",
    "random_page_cost", "seq_page_cost", "jit", "max_parallel_workers_per_gather", "max_connections",
    "autovacuum", "default_statistics_target", "shared_preload_libraries", "track_io_timing",
    "statement_timeout", "lock_timeout", "default_transaction_read_only", "data_checksums",
)

# pg_depend catalogs whose rows against a table are understood: index and
# sequence ownership, constraints, defaults, rules (views), triggers, row
# types, policies, publications, extended statistics. Anything else is
# reported as an unverified dependency, which the policy treats as a block.
_KNOWN_DEPENDENCY_CATALOGS = {
    "pg_class", "pg_constraint", "pg_attrdef", "pg_rewrite", "pg_trigger", "pg_type",
    "pg_policy", "pg_publication_rel", "pg_statistic_ext",
}

_PREDICATE = re.compile(
    r"""^\(*\s*(?:[a-z_][a-z0-9_]*\.)?(?P<col>[a-z_][a-z0-9_]*)\s*\)*   # column, optionally qualified / parenthesised
        (?:::[a-z ]+(?:\[\])?)?\s*                                        # optional cast on the column side
        (?P<op>=|<>|!=|<=|>=|<|>)\s*(?P<rhs>.+?)\)*$""",
    re.IGNORECASE | re.VERBOSE,
)
_LITERAL_RHS = re.compile(r"^\(*\s*('|\$\d|-?\d|true\b|false\b|ANY\s*\()", re.IGNORECASE)

_INDEX_STATE_SELECT = """
    SELECT n.nspname AS schema, i.relname AS name, t.relname AS table, x.indisvalid AS valid, x.indisready AS ready,
           pg_get_indexdef(i.oid) AS definition, obj_description(i.oid, 'pg_class') AS comment,
           pg_relation_size(i.oid) AS size_bytes, i.oid::bigint AS oid, am.amname AS method,
           x.indisunique AS is_unique, (x.indexprs IS NOT NULL) AS has_expressions, (x.indpred IS NOT NULL) AS is_partial,
           ARRAY(SELECT a.attname FROM unnest(x.indkey) WITH ORDINALITY k(attnum, ord)
                 JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = k.attnum ORDER BY k.ord)::text[] AS columns
    FROM pg_class i JOIN pg_index x ON x.indexrelid = i.oid JOIN pg_class t ON t.oid = x.indrelid
    JOIN pg_namespace n ON n.oid = i.relnamespace JOIN pg_am am ON am.oid = i.relam
"""


def application_name(identity: str, operation_id: str | None) -> str:
    """fh-dbeng/<identity>/<operation>: how server-side activity is traced back to its origin."""
    return f"{APPLICATION_PREFIX}/{identity}/{operation_id or 'idle'}"[:63]


def parse_application_name(name: str | None) -> tuple[str, str] | None:
    parts = (name or "").split("/")
    if len(parts) == 3 and parts[0] == APPLICATION_PREFIX:
        return parts[1], parts[2]
    return None


def parse_filter(filter_text: str) -> list[dict]:
    """
    Column predicates of a plan node's Filter, as [{column, op}] -- only the
    shapes an ordinary b-tree on plain columns can serve: AND-ed comparisons
    of a column against a literal or parameter. Anything else (OR, function
    calls on the column, column-to-column comparisons) yields [] rather
    than a guess.
    """
    text = filter_text.strip()
    if re.search(r"\bOR\b", text, re.IGNORECASE):
        return []
    while text.startswith("(") and text.endswith(")") and _balanced(text[1:-1]):
        text = text[1:-1].strip()
    predicates = []
    for part in re.split(r"\)\s+AND\s+\(", text, flags=re.IGNORECASE):
        match = _PREDICATE.match(part.strip())
        if not match or not _LITERAL_RHS.match(match.group("rhs")):
            return []
        op = match.group("op")
        if op in ("<>", "!="):
            return []
        predicates.append({"column": match.group("col").lower(), "op": op})
    return predicates


def _balanced(text: str) -> bool:
    depth = 0
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if depth < 0:
            return False
    return depth == 0


def walk_plan(node: dict, acc: dict) -> None:
    acc["node_types"].append(node.get("Node Type", "?"))
    if node.get("Index Name"):
        acc["indexes_used"].append(node["Index Name"])
    if node.get("Node Type") == "Seq Scan" and node.get("Relation Name"):
        loops = max(1, int(node.get("Actual Loops", 1) or 1))
        rows = float(node.get("Actual Rows", 0) or 0) * loops
        removed = float(node.get("Rows Removed by Filter", 0) or 0) * loops
        scanned = rows + removed
        acc["seq_scans"].append({
            "schema": node.get("Schema"),
            "relation": node["Relation Name"],
            "filter": node.get("Filter"),
            "predicates": parse_filter(node["Filter"]) if node.get("Filter") else [],
            "rows_returned": rows,
            "rows_removed_by_filter": removed,
            "selectivity": (rows / scanned) if scanned else None,
        })
    for child in node.get("Plans", []) or []:
        walk_plan(child, acc)


def explain_result_from_json(doc: list | dict) -> ExplainResult:
    root = doc[0] if isinstance(doc, list) else doc
    acc: dict = {"node_types": [], "indexes_used": [], "seq_scans": []}
    walk_plan(root["Plan"], acc)
    return ExplainResult(
        execution_ms=float(root.get("Execution Time", 0.0)),
        planning_ms=float(root.get("Planning Time", 0.0)),
        indexes_used=acc["indexes_used"],
        node_types=acc["node_types"],
        seq_scans=acc["seq_scans"],
        plan=root,
    )


class PostgresAdapter:
    engine = "postgresql"

    def __init__(self, target: TargetConfig, operation_id: str | None = None):
        self.target = target
        self.operation_id = operation_id
        self._observer: psycopg.Connection[dict[str, Any]] | None = None
        self._measure: psycopg.Connection[dict[str, Any]] | None = None

    def bind_operation(self, operation_id: str | None) -> None:
        """Tags this adapter's sessions with the operation (closing sessions opened under another tag)."""
        if operation_id != self.operation_id:
            self.close()
            self.operation_id = operation_id

    # --- connections -------------------------------------------------------
    def _conninfo(self, user: str, identity: str, read_only: bool) -> str:
        options = "-c default_transaction_read_only=on -c statement_timeout=120000" if read_only else "-c statement_timeout=300000"
        params: dict[str, Any] = {
            "host": self.target.host,
            "port": self.target.port,
            "dbname": self.target.dbname,
            "user": user,
            "sslmode": self.target.sslmode,
            "application_name": application_name(identity, self.operation_id),
            "connect_timeout": 10,
            "options": options,
        }
        if self.target.passfile:
            params["passfile"] = self.target.passfile
        return make_conninfo(**params)

    def _connect(self, user: str, identity: str, read_only: bool) -> psycopg.Connection[dict[str, Any]]:
        try:
            return psycopg.connect(self._conninfo(user, identity, read_only), autocommit=True, row_factory=dict_row)
        except psycopg.OperationalError as exc:
            raise ConnectionError(redact(str(exc)).strip()) from None

    @property
    def observer(self) -> psycopg.Connection[dict[str, Any]]:
        if self._observer is None or self._observer.closed:
            self._observer = self._connect(self.target.observer_user, "observer", read_only=True)
        return self._observer

    @property
    def measure(self) -> psycopg.Connection[dict[str, Any]]:
        if not self.target.measure_user:
            raise PermissionError(
                f"Target {self.target.name!r} declares no measurement identity (measure_user); "
                "workload measurement never borrows the operator's rights."
            )
        if self._measure is None or self._measure.closed:
            self._measure = self._connect(self.target.measure_user, "measure", read_only=False)
        return self._measure

    def close(self) -> None:
        for conn in (self._observer, self._measure):
            if conn is not None and not conn.closed:
                conn.close()
        self._observer = self._measure = None

    def _rows(self, query, params=None, conn: psycopg.Connection[dict[str, Any]] | None = None) -> list[dict]:
        with (conn or self.observer).cursor() as cur:
            cur.execute(query, params)
            return list(cur.fetchall()) if cur.description else []

    def _optional(self, query, params=None) -> tuple[str, list[dict], str]:
        """Runs a query whose availability depends on privileges/extensions; never raises."""
        try:
            return AVAILABLE, self._rows(query, params), ""
        except psycopg.Error as exc:
            return UNAVAILABLE, [], redact(str(exc).strip().splitlines()[0])

    # --- observation ---------------------------------------------------------
    def identity(self) -> dict:
        row = self._rows(
            "SELECT version() AS version, current_setting('server_version_num')::int AS version_num, "
            "current_database() AS database, current_user AS observer, pg_is_in_recovery() AS in_recovery, "
            "inet_server_addr()::text AS server_addr, inet_server_port() AS server_port"
        )[0]
        status, sysid_rows, sysid_detail = self._optional("SELECT system_identifier::text AS sysid FROM pg_control_system()")
        marker_status, marker_rows, marker_detail = self._optional(
            "SELECT marker::text AS marker, created_at::text AS created_at FROM fieldhorizon_meta.instance_marker"
        )
        return {
            **row,
            "engine": self.engine,
            "system_identifier": sysid_rows[0]["sysid"] if sysid_rows else None,
            "system_identifier_status": status if sysid_rows else UNAVAILABLE,
            "system_identifier_detail": sysid_detail,
            "instance_marker": marker_rows[0]["marker"] if marker_rows else None,
            "instance_marker_status": marker_status if marker_rows else UNAVAILABLE,
            "instance_marker_detail": marker_detail or ("" if marker_rows else "no fieldhorizon_meta.instance_marker row"),
        }

    def discover(self) -> dict:
        availability: dict[str, dict] = {}
        settings = self._rows(
            "SELECT name, setting, unit, source FROM pg_settings WHERE name = ANY(%s) ORDER BY name",
            (list(RELEVANT_SETTINGS),),
        )
        extensions = self._rows("SELECT extname AS name, extversion AS version FROM pg_extension ORDER BY 1")
        installed = {e["name"] for e in extensions}
        available_ext = {r["name"] for r in self._rows("SELECT name FROM pg_available_extensions")}
        for ext, purpose in (("pg_stat_statements", "workload statistics"), ("hypopg", "hypothetical indexes")):
            if ext in installed:
                availability[ext] = {"status": AVAILABLE, "detail": purpose}
            elif ext in available_ext:
                availability[ext] = {"status": UNAVAILABLE, "detail": f"{purpose}: shipped but not installed in this database"}
            else:
                availability[ext] = {"status": UNAVAILABLE, "detail": f"{purpose}: not shipped with this server build"}

        schemas = self._rows(
            "SELECT nspname AS schema, nspowner::regrole::text AS owner FROM pg_namespace "
            "WHERE nspname <> ALL(%s) AND nspname NOT LIKE 'pg\\_temp\\_%%' AND nspname NOT LIKE 'pg\\_toast\\_temp\\_%%' ORDER BY 1",
            (list(SYSTEM_SCHEMAS),),
        )
        tables = self._rows(
            """
            SELECT n.nspname AS schema, c.relname AS table, c.relkind AS kind, c.oid::bigint AS oid,
                   c.relowner::regrole::text AS owner, c.reltuples::bigint AS estimated_rows, c.relpages::bigint AS pages,
                   pg_total_relation_size(c.oid) AS total_bytes, pg_relation_size(c.oid) AS heap_bytes,
                   c.relispartition AS is_partition
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'm') AND n.nspname <> ALL(%s)
            ORDER BY 1, 2
            """,
            (list(SYSTEM_SCHEMAS),),
        )
        columns = self._rows(
            """
            SELECT n.nspname AS schema, c.relname AS table, a.attnum AS position, a.attname AS column,
                   format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS not_null,
                   pg_get_expr(d.adbin, d.adrelid) AS default
            FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'm') AND n.nspname <> ALL(%s)
            ORDER BY 1, 2, 3
            """,
            (list(SYSTEM_SCHEMAS),),
        )
        indexes = self._rows(
            """
            SELECT n.nspname AS schema, t.relname AS table, i.relname AS index, am.amname AS method,
                   x.indisunique AS is_unique, x.indisprimary AS is_primary, x.indisvalid AS is_valid,
                   x.indisready AS is_ready, (x.indexprs IS NOT NULL) AS has_expressions,
                   (x.indpred IS NOT NULL) AS is_partial,
                   ARRAY(SELECT a.attname FROM unnest(x.indkey) WITH ORDINALITY k(attnum, ord)
                         JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = k.attnum ORDER BY k.ord)::text[] AS columns,
                   pg_get_indexdef(i.oid) AS definition, pg_relation_size(i.oid) AS bytes,
                   obj_description(i.oid, 'pg_class') AS comment,
                   s.idx_scan, s.idx_tup_read
            FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_am am ON am.oid = i.relam
            LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.oid
            WHERE n.nspname <> ALL(%s)
            ORDER BY 1, 2, 3
            """,
            (list(SYSTEM_SCHEMAS),),
        )
        constraints = self._rows(
            """
            SELECT n.nspname AS schema, t.relname AS table, con.conname AS name, con.contype AS type,
                   ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
                         JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum ORDER BY k.ord)::text[] AS columns,
                   con.confrelid::regclass::text AS references, pg_get_constraintdef(con.oid) AS definition,
                   con.convalidated AS validated
            FROM pg_constraint con JOIN pg_class t ON t.oid = con.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname <> ALL(%s)
            ORDER BY 1, 2, 3
            """,
            (list(SYSTEM_SCHEMAS),),
        )
        views = self._rows(
            """
            SELECT DISTINCT vn.nspname AS view_schema, v.relname AS view, v.relkind AS view_kind,
                   tn.nspname AS schema, t.relname AS table
            FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid
            JOIN pg_class v ON v.oid = r.ev_class JOIN pg_namespace vn ON vn.oid = v.relnamespace
            JOIN pg_class t ON t.oid = d.refobjid JOIN pg_namespace tn ON tn.oid = t.relnamespace
            WHERE d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass
              AND d.refobjid <> v.oid AND tn.nspname <> ALL(%s)
            ORDER BY 1, 2
            """,
            (list(SYSTEM_SCHEMAS),),
        )
        table_stats_status, table_stats, table_stats_detail = self._optional(
            """
            SELECT schemaname AS schema, relname AS table, seq_scan, seq_tup_read, idx_scan, n_live_tup, n_dead_tup,
                   last_analyze::text, last_autoanalyze::text, last_vacuum::text, last_autovacuum::text
            FROM pg_stat_user_tables ORDER BY 1, 2
            """
        )
        availability["table_statistics"] = {"status": table_stats_status, "detail": table_stats_detail}
        column_stats_status, column_stats, column_stats_detail = self._optional(
            """
            SELECT schemaname AS schema, tablename AS table, attname AS column, null_frac, n_distinct, avg_width,
                   (most_common_freqs)[1] AS top_frequency
            FROM pg_stats WHERE schemaname <> ALL(%s) ORDER BY 1, 2, 3
            """,
            (list(SYSTEM_SCHEMAS),),
        )
        availability["column_statistics"] = {"status": column_stats_status, "detail": column_stats_detail}
        if "pg_stat_statements" in installed:
            observed = self.statement_statistics()
            statements = observed["statements"]
            availability["pg_stat_statements"] = {
                "status": observed["status"],
                "detail": observed["detail"] or "observed traffic, normalised: informs diagnosis, never replayed",
            }
        else:
            statements = []

        analyzed = {(r["schema"], r["table"]) for r in column_stats}
        for t in tables:
            t["column_statistics"] = AVAILABLE if (t["schema"], t["table"]) in analyzed else UNAVAILABLE
            t["kind"] = {"r": "table", "p": "partitioned_table", "m": "materialized_view"}.get(t["kind"], t["kind"])

        return {
            "captured_at": datetime.now(UTC).isoformat(),
            "settings": settings,
            "extensions": extensions,
            "availability": availability,
            "schemas": schemas,
            "tables": tables,
            "columns": columns,
            "indexes": indexes,
            "constraints": constraints,
            "views": views,
            "table_statistics": table_stats,
            "column_statistics": column_stats,
            "statements": statements,
        }

    # --- measurement (measure identity) ----------------------------------------
    def _measure_conn(self, write: bool) -> psycopg.Connection[dict[str, Any]]:
        if self.target.measure_user:
            return self.measure
        if write:
            raise PermissionError(f"Target {self.target.name!r}: write queries need a measurement identity (measure_user).")
        return self.observer

    def explain(self, query: str, params: dict | None = None, *, write: bool = False) -> ExplainResult:
        """
        EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, VERBOSE, FORMAT JSON) under the
        measurement identity: the server's own execution time, free of network
        and client overhead, plus the plan that produced it. Parameters are
        inlined client-side so the planner sees literal values (a custom plan),
        exactly as for a one-off execution. A write statement is executed inside
        a transaction that is always rolled back.
        """
        statement = sql.SQL("EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, VERBOSE, FORMAT JSON) ") + sql.SQL(query)  # type: ignore[arg-type,unused-ignore]
        conn = self._measure_conn(write)
        with psycopg.ClientCursor(conn) as cur:
            if write:
                with conn.transaction(force_rollback=True):
                    cur.execute(statement, params)
                    doc = cur.fetchone()
            else:
                cur.execute(statement, params)
                doc = cur.fetchone()
        assert doc is not None
        value = doc["QUERY PLAN"] if isinstance(doc, dict) else doc[0]
        return explain_result_from_json(value if not isinstance(value, str) else json.loads(value))

    def execute_timed(self, query: str, params: dict | None = None, *, write: bool = False) -> float:
        """
        Client-side latency of the plain statement (send, execute, fetch every
        row) in ms, under the measurement identity: what an application on
        this connection would see. Writes are rolled back.
        """
        conn = self._measure_conn(write)
        with psycopg.ClientCursor(conn) as cur:
            if write:
                with conn.transaction(force_rollback=True):
                    started = time.perf_counter()
                    cur.execute(query, params)  # type: ignore[arg-type,unused-ignore]
                    if cur.description:
                        cur.fetchall()
                    elapsed = time.perf_counter() - started
            else:
                started = time.perf_counter()
                cur.execute(query, params)  # type: ignore[arg-type,unused-ignore]
                if cur.description:
                    cur.fetchall()
                elapsed = time.perf_counter() - started
        return elapsed * 1000.0

    # --- objects -------------------------------------------------------------------
    def table_facts(self, schema: str, table: str) -> dict:
        rows = self._rows(
            """
            SELECT c.oid::bigint AS oid, c.relkind AS kind, c.reltuples::bigint AS estimated_rows,
                   c.relowner::regrole::text AS owner, pg_total_relation_size(c.oid) AS total_bytes,
                   c.relispartition AS is_partition,
                   (SELECT count(*) FROM pg_index x WHERE x.indrelid = c.oid) AS index_count,
                   (SELECT count(*) FROM pg_inherits h WHERE h.inhparent = c.oid) AS child_count
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
            """,
            (schema, table),
        )
        if not rows:
            return {"exists": False}
        facts = {"exists": True, **rows[0]}
        facts["kind"] = {"r": "table", "p": "partitioned_table", "m": "materialized_view", "v": "view", "f": "foreign_table"}.get(
            facts["kind"], facts["kind"]
        )
        facts["columns"] = [r["attname"] for r in self._rows(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
            (facts["oid"],),
        )]
        facts["indexes"] = self._rows(
            """
            SELECT i.relname AS index, x.indisvalid AS is_valid, (x.indexprs IS NOT NULL) AS has_expressions,
                   (x.indpred IS NOT NULL) AS is_partial, am.amname AS method,
                   ARRAY(SELECT a.attname FROM unnest(x.indkey) WITH ORDINALITY k(attnum, ord)
                         JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = k.attnum ORDER BY k.ord)::text[] AS columns
            FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_am am ON am.oid = i.relam
            WHERE x.indrelid = %s ORDER BY 1
            """,
            (facts["oid"],),
        )
        deps = self._rows(
            """
            SELECT d.classid::regclass::text AS catalog, d.deptype::text AS deptype, count(*) AS n
            FROM pg_depend d WHERE d.refclassid = 'pg_class'::regclass AND d.refobjid = %s GROUP BY 1, 2 ORDER BY 1, 2
            """,
            (facts["oid"],),
        )
        facts["dependencies"] = deps
        facts["unverified_dependencies"] = [d for d in deps if d["catalog"] not in _KNOWN_DEPENDENCY_CATALOGS]
        facts["dependent_views"] = [r["view"] for r in self._rows(
            """
            SELECT DISTINCT v.oid::regclass::text AS view FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid
            JOIN pg_class v ON v.oid = r.ev_class
            WHERE d.classid = 'pg_rewrite'::regclass AND d.refobjid = %s AND v.oid <> %s
            """,
            (facts["oid"], facts["oid"]),
        )]
        facts["operator_can_create_index"] = self._operator_reaches_owner(facts["owner"])
        facts["partitioning"] = UNSUPPORTED if facts["kind"] == "partitioned_table" or facts["child_count"] else AVAILABLE
        facts["dependency_status"] = UNVERIFIED if facts["unverified_dependencies"] else AVAILABLE
        return facts

    def _operator_reaches_owner(self, owner: str | None) -> bool:
        """
        The operator can build an index on a table only through SET ROLE to its
        owner: membership WITH SET, and the owner must be the declared owner_role
        (a table owned by anyone else is out of reach by design).
        """
        if not self.target.operator_user or not owner:
            return False
        if self.target.owner_role and owner != self.target.owner_role:
            return False
        rows = self._rows("SELECT pg_has_role(%s, %s, 'SET') AS can_set", (self.target.operator_user, owner))
        return bool(rows and rows[0]["can_set"])

    def column_statistics(self, schema: str, table: str, columns: list[str]) -> dict[str, dict]:
        rows = self._rows(
            """
            SELECT attname AS column, null_frac, n_distinct, avg_width, most_common_freqs::float8[] AS mcf
            FROM pg_stats WHERE schemaname = %s AND tablename = %s AND attname = ANY(%s)
            """,
            (schema, table, columns),
        )
        return {r["column"]: {k: v for k, v in r.items() if k != "column"} for r in rows}

    def running_transactions(self) -> list[dict]:
        return self._rows(
            """
            SELECT pid, usename, application_name, state, extract(epoch FROM now() - xact_start)::float8 AS xact_seconds,
                   backend_xmin IS NOT NULL AS holds_snapshot
            FROM pg_stat_activity
            WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid() AND datname = current_database()
              AND backend_type = 'client backend'
            ORDER BY xact_start
            """
        )

    def index_state(self, schema: str, name: str) -> IndexState | None:
        rows = self._rows(_INDEX_STATE_SELECT + " WHERE n.nspname = %s AND i.relname = %s", (schema, name))
        return _index_state(rows[0]) if rows else None

    def fieldhorizon_indexes(self) -> list[IndexState]:
        rows = self._rows(
            _INDEX_STATE_SELECT + " WHERE i.relname LIKE 'fhx\\_%%' OR obj_description(i.oid, 'pg_class') LIKE %s ORDER BY 1, 2",
            (f'%"{MARKER_KEY}": "{MARKER_VALUE}"%',),
        )
        return [_index_state(r) for r in rows]

    def invalid_indexes(self) -> list[dict]:
        return self._rows(
            "SELECT n.nspname AS schema, i.relname AS index, t.relname AS table FROM pg_index x "
            "JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid "
            "JOIN pg_namespace n ON n.oid = i.relnamespace WHERE NOT x.indisvalid AND n.nspname <> ALL(%s) ORDER BY 1, 2",
            (list(SYSTEM_SCHEMAS),),
        )

    # --- coordination and server activity (observer, read-only) ---------------------
    def lease_state(self, scope: str) -> dict:
        """The target-side lease row, and whether its advisory lock is held right now -- and by which backend."""
        status, rows, detail = self._optional(
            "SELECT scope, fencing_token, holder_operation_id, holder_host, holder_pid, holder_backend_pid, holder_journal, "
            "acquired_at::text, heartbeat_at::text, released_at::text, "
            "extract(epoch FROM now() - heartbeat_at)::float8 AS heartbeat_age_seconds "
            "FROM fieldhorizon_meta.operation_lease WHERE scope = %s",
            (scope,),
        )
        lock_rows = self._rows(
            "SELECT l.pid, a.application_name, a.state, a.backend_start::text, left(a.query, 200) AS query FROM pg_locks l "
            "LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 2 "
            "AND l.classid = hashtext(%s)::oid AND l.objid = hashtext(%s)::oid",
            (LOCK_NAMESPACE, scope),
        )
        return {
            "status": status,
            "detail": detail,
            "row": rows[0] if rows else None,
            "lock_held": bool(lock_rows),
            "lock_holder": lock_rows[0] if lock_rows else None,
        }

    def ledger_rows(self, *, unfinished_only: bool = False) -> list[dict]:
        clause = " WHERE state NOT IN ('retained', 'reverted')" if unfinished_only else ""
        status, rows, _ = self._optional(
            "SELECT operation_id, change_id, fencing_token, action, index_schema, index_name, table_name, columns, definition, "
            "index_oid, state, holder_host, holder_journal, recovered_by, note, created_at::text, updated_at::text "
            "FROM fieldhorizon_meta.change_ledger" + clause + " ORDER BY created_at"
        )
        return rows if status == AVAILABLE else []

    def fieldhorizon_activity(self) -> list[dict]:
        """Every live session Field Horizon started on this database, whatever process or journal started it."""
        rows = self._rows(
            """
            SELECT pid, usename, application_name, state, wait_event_type, wait_event, backend_start::text,
                   query_start::text, xact_start::text,
                   extract(epoch FROM now() - query_start)::float8 AS query_seconds, left(query, 300) AS query
            FROM pg_stat_activity
            WHERE application_name LIKE %s AND pid <> pg_backend_pid() AND datname = current_database()
            ORDER BY backend_start
            """,
            (f"{APPLICATION_PREFIX}/%",),
        )
        for r in rows:
            parsed = parse_application_name(r["application_name"])
            r["identity"], r["operation_id"] = parsed if parsed else (None, None)
        return rows

    def index_build_progress(self) -> list[dict]:
        status, rows, _ = self._optional(
            "SELECT p.pid, p.relid::regclass::text AS table, p.index_relid::regclass::text AS index, p.phase, "
            "p.blocks_total, p.blocks_done, p.tuples_total, p.tuples_done, a.application_name "
            "FROM pg_stat_progress_create_index p LEFT JOIN pg_stat_activity a ON a.pid = p.pid"
        )
        return rows if status == AVAILABLE else []

    def state_snapshot(self, schemas: tuple[str, ...] = ("bench",)) -> dict:
        """
        The measurable state of the lab around a measurement: live and dead
        tuples, sizes, maintenance counters, identity-sequence positions.
        Measurement writes are rolled back, yet they still leave dead tuples and
        consume identity values: this is where that side effect is shown.
        """
        tables = self._rows(
            """
            SELECT s.schemaname AS schema, s.relname AS table, s.n_live_tup, s.n_dead_tup, s.n_tup_ins, s.n_tup_upd,
                   s.n_tup_del, s.seq_scan, s.idx_scan, s.autovacuum_count, s.autoanalyze_count, s.vacuum_count,
                   s.analyze_count, s.last_autovacuum::text, s.last_autoanalyze::text,
                   pg_relation_size(s.relid) AS heap_bytes, pg_indexes_size(s.relid) AS index_bytes,
                   pg_total_relation_size(s.relid) AS total_bytes
            FROM pg_stat_user_tables s WHERE s.schemaname = ANY(%s) ORDER BY 1, 2
            """,
            (list(schemas),),
        )
        seq_status, sequences, seq_detail = self._optional(
            "SELECT schemaname AS schema, sequencename AS sequence, last_value FROM pg_sequences WHERE schemaname = ANY(%s) ORDER BY 1, 2",
            (list(schemas),),
        )
        db = self._rows("SELECT pg_database_size(current_database()) AS database_bytes, now()::text AS at")[0]
        return {
            "captured_at": datetime.now(UTC).isoformat(),
            "server_time": db["at"],
            "database_bytes": db["database_bytes"],
            "tables": tables,
            "sequences": sequences,
            "sequences_status": seq_status,
            "sequences_detail": seq_detail,
            "maintenance": self.maintenance_activity(),
        }

    def maintenance_activity(self) -> list[dict]:
        """VACUUM / ANALYZE running right now in this database (autovacuum included): measuring through it measures it."""
        status, rows, _ = self._optional(
            """
            SELECT a.pid, a.backend_type, left(a.query, 200) AS query, a.query_start::text,
                   extract(epoch FROM now() - a.query_start)::float8 AS seconds,
                   v.relid::regclass::text AS vacuum_relation, v.phase AS vacuum_phase,
                   z.relid::regclass::text AS analyze_relation, z.phase AS analyze_phase
            FROM pg_stat_activity a
            LEFT JOIN pg_stat_progress_vacuum v ON v.pid = a.pid
            LEFT JOIN pg_stat_progress_analyze z ON z.pid = a.pid
            WHERE a.datname = current_database() AND a.pid <> pg_backend_pid()
              AND (a.backend_type = 'autovacuum worker' OR v.pid IS NOT NULL OR z.pid IS NOT NULL)
            """
        )
        return rows if status == AVAILABLE else []

    def authority_facts(self, schemas: tuple[str, ...] = ("bench",)) -> dict:
        """
        The privilege boundary as the server actually enforces it, read live from
        the catalogs: role attributes, the operator's membership options, the
        guards, table owners and per-identity table privileges.
        """
        roles = [r for r in (self.target.observer_user, self.target.measure_user, self.target.operator_user, self.target.owner_role) if r]
        attributes = self._rows(
            "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolinherit, rolcanlogin, "
            "coalesce(rolconfig, '{}')::text[] AS settings FROM pg_roles WHERE rolname = ANY(%s) ORDER BY 1",
            (roles,),
        )
        status, memberships, detail = self._optional(
            "SELECT r.rolname AS member, g.rolname AS role, m.inherit_option, m.set_option, m.admin_option "
            "FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member JOIN pg_roles g ON g.oid = m.roleid "
            "WHERE r.rolname = ANY(%s) ORDER BY 1, 2",
            (roles,),
        )
        event_triggers = self._rows(
            "SELECT evtname AS name, evtevent AS event, evtenabled::text AS enabled, evtfoid::regproc::text AS function "
            "FROM pg_event_trigger ORDER BY 1"
        )
        dml_guards = self._rows(
            "SELECT tgrelid::regclass::text AS table, tgname AS name, tgenabled::text AS enabled FROM pg_trigger "
            "WHERE tgname = 'fieldhorizon_guard_operator_dml' ORDER BY 1"
        )
        tables = self._rows(
            "SELECT n.nspname AS schema, c.relname AS table, c.relowner::regrole::text AS owner FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = ANY(%s) AND c.relkind = 'r' ORDER BY 1, 2",
            (list(schemas),),
        )
        privileges = []
        for t in tables:
            qualified = f"{t['schema']}.{t['table']}"
            for role in roles:
                row = self._rows(
                    "SELECT has_table_privilege(%(r)s, %(t)s, 'SELECT') AS select, has_table_privilege(%(r)s, %(t)s, 'INSERT') AS insert, "
                    "has_table_privilege(%(r)s, %(t)s, 'UPDATE') AS update, has_table_privilege(%(r)s, %(t)s, 'DELETE') AS delete, "
                    "has_table_privilege(%(r)s, %(t)s, 'TRUNCATE') AS truncate",
                    {"r": role, "t": qualified},
                )[0]
                privileges.append({"table": qualified, "role": role, **row})
        return {"roles": attributes, "memberships": memberships, "memberships_status": status, "memberships_detail": detail,
                "event_triggers": event_triggers, "dml_guards": dml_guards, "tables": tables, "privileges": privileges}

    def statement_statistics(self, limit: int = 25) -> dict:
        """pg_stat_statements as observed traffic: normalised, so it informs diagnosis; it is never replayed."""
        status, rows, detail = self._optional(
            """
            SELECT queryid::text, left(query, 300) AS query, calls, round(total_exec_time::numeric, 3) AS total_ms,
                   round(mean_exec_time::numeric, 3) AS mean_ms, rows, shared_blks_hit, shared_blks_read
            FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
              -- defensive: never carry a role-password statement into a snapshot
              AND query NOT ILIKE '%%password%%'
            ORDER BY total_exec_time DESC LIMIT %s
            """,
            (limit,),
        )
        return {"status": status, "detail": detail, "statements": rows}


def parse_marker(comment: str | None) -> dict | None:
    if not comment:
        return None
    try:
        parsed = json.loads(comment)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) and parsed.get(MARKER_KEY) == MARKER_VALUE else None


def _index_state(row: dict) -> IndexState:
    return IndexState(
        schema=row["schema"], name=row["name"], table=row["table"], valid=bool(row["valid"]), ready=bool(row["ready"]),
        definition=row["definition"], marker=parse_marker(row.get("comment")), size_bytes=int(row["size_bytes"] or 0),
        oid=int(row["oid"]) if row.get("oid") is not None else None, method=row.get("method"),
        columns=tuple(row.get("columns") or ()), is_unique=bool(row.get("is_unique")),
        has_expressions=bool(row.get("has_expressions")), is_partial=bool(row.get("is_partial")),
    )
