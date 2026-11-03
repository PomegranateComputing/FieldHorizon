"""
PostgreSQL adapter (psycopg 3).

Two identities, two connections, both lazily opened:

* observer -- discovery, plans, statistics, timing of read queries. Its
  connections run with default_transaction_read_only=on, on top of the
  role-level default the bench setup applies.
* operator -- CREATE/DROP INDEX CONCURRENTLY, ANALYZE, and timing of write
  queries inside a transaction that is always rolled back. Autocommit:
  PostgreSQL refuses CREATE INDEX CONCURRENTLY inside a transaction block.

Every identifier reaching SQL is composed with psycopg.sql, never with
string formatting. Statistics that cannot be read are reported with an
availability status, never defaulted to zero.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from ..model import AVAILABLE, UNAVAILABLE, UNSUPPORTED, UNVERIFIED, ExplainResult, IndexSpec, IndexState
from ..targets import TargetConfig, redact

APPLICATION_NAME = "fieldhorizon-dbeng"
MARKER_KEY = "fieldhorizon"
MARKER_VALUE = "dbeng"
ALLOWED_METHODS = {"btree"}

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

    def __init__(self, target: TargetConfig):
        self.target = target
        self._observer: psycopg.Connection[dict[str, Any]] | None = None
        self._operator: psycopg.Connection[dict[str, Any]] | None = None

    # --- connections -------------------------------------------------------
    def _conninfo(self, user: str, read_only: bool) -> str:
        options = "-c default_transaction_read_only=on -c statement_timeout=120000" if read_only else ""
        params: dict[str, Any] = {
            "host": self.target.host,
            "port": self.target.port,
            "dbname": self.target.dbname,
            "user": user,
            "sslmode": self.target.sslmode,
            "application_name": APPLICATION_NAME,
            "connect_timeout": 10,
        }
        if self.target.passfile:
            params["passfile"] = self.target.passfile
        if options:
            params["options"] = options
        return make_conninfo(**params)

    def _connect(self, user: str, read_only: bool) -> psycopg.Connection[dict[str, Any]]:
        try:
            return psycopg.connect(self._conninfo(user, read_only), autocommit=True, row_factory=dict_row)
        except psycopg.OperationalError as exc:
            raise ConnectionError(redact(str(exc)).strip()) from None

    @property
    def observer(self) -> psycopg.Connection[dict[str, Any]]:
        if self._observer is None or self._observer.closed:
            self._observer = self._connect(self.target.observer_user, read_only=True)
        return self._observer

    @property
    def operator(self) -> psycopg.Connection[dict[str, Any]]:
        if not self.target.operator_user:
            raise PermissionError(f"Target {self.target.name!r} declares no operator identity; it is read-only.")
        if self._operator is None or self._operator.closed:
            self._operator = self._connect(self.target.operator_user, read_only=False)
        return self._operator

    def close(self) -> None:
        for conn in (self._observer, self._operator):
            if conn is not None and not conn.closed:
                conn.close()

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
            st, statements, detail = self._optional(
                """
                SELECT queryid::text, left(query, 500) AS query, calls, round(total_exec_time::numeric, 3) AS total_ms,
                       round(mean_exec_time::numeric, 3) AS mean_ms, rows, shared_blks_hit, shared_blks_read
                FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                  -- defensive: never carry a role-password statement into a snapshot
                  AND query NOT ILIKE '%password%'
                ORDER BY total_exec_time DESC LIMIT 25
                """
            )
            availability["pg_stat_statements"] = {"status": st, "detail": detail or availability["pg_stat_statements"]["detail"]}
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

    def explain(self, query: str, params: dict | None = None, *, write: bool = False) -> ExplainResult:
        """
        EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, VERBOSE, FORMAT JSON): the
        server's own execution time, free of network and client overhead,
        plus the plan that produced it. Parameters are inlined client-side
        so the planner sees literal values (a custom plan), exactly as for
        a one-off execution. A write statement is executed inside a
        transaction that is always rolled back, so measuring it changes
        nothing.
        """
        statement = sql.SQL("EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, VERBOSE, FORMAT JSON) ") + sql.SQL(query)  # type: ignore[arg-type,unused-ignore]
        conn = self.operator if write else self.observer
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
        if self.target.operator_user:
            priv = self._rows(
                "SELECT pg_has_role(%s, c.relowner, 'USAGE') AS operator_can_create_index FROM pg_class c WHERE c.oid = %s",
                (self.target.operator_user, facts["oid"]),
            )
            facts["operator_can_create_index"] = bool(priv[0]["operator_can_create_index"]) if priv else False
        else:
            facts["operator_can_create_index"] = False
        facts["partitioning"] = UNSUPPORTED if facts["kind"] == "partitioned_table" or facts["child_count"] else AVAILABLE
        facts["dependency_status"] = UNVERIFIED if facts["unverified_dependencies"] else AVAILABLE
        return facts

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
            SELECT pid, usename, application_name, state, extract(epoch FROM now() - xact_start)::float8 AS xact_seconds
            FROM pg_stat_activity
            WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid() AND datname = current_database()
              AND backend_type = 'client backend'
            ORDER BY xact_start
            """
        )

    def index_state(self, schema: str, name: str) -> IndexState | None:
        rows = self._rows(
            """
            SELECT n.nspname AS schema, i.relname AS name, t.relname AS table, x.indisvalid AS valid, x.indisready AS ready,
                   pg_get_indexdef(i.oid) AS definition, obj_description(i.oid, 'pg_class') AS comment,
                   pg_relation_size(i.oid) AS size_bytes
            FROM pg_class i JOIN pg_index x ON x.indexrelid = i.oid JOIN pg_class t ON t.oid = x.indrelid
            JOIN pg_namespace n ON n.oid = i.relnamespace
            WHERE n.nspname = %s AND i.relname = %s
            """,
            (schema, name),
        )
        return _index_state(rows[0]) if rows else None

    def fieldhorizon_indexes(self) -> list[IndexState]:
        rows = self._rows(
            """
            SELECT n.nspname AS schema, i.relname AS name, t.relname AS table, x.indisvalid AS valid, x.indisready AS ready,
                   pg_get_indexdef(i.oid) AS definition, obj_description(i.oid, 'pg_class') AS comment,
                   pg_relation_size(i.oid) AS size_bytes
            FROM pg_class i JOIN pg_index x ON x.indexrelid = i.oid JOIN pg_class t ON t.oid = x.indrelid
            JOIN pg_namespace n ON n.oid = i.relnamespace
            WHERE i.relname LIKE 'fhx\\_%%' OR obj_description(i.oid, 'pg_class') LIKE %s
            ORDER BY 1, 2
            """,
            (f'%"{MARKER_KEY}": "{MARKER_VALUE}"%',),
        )
        return [_index_state(r) for r in rows]

    # --- change ----------------------------------------------------------------
    def _session_timeouts(self, cur, lock_timeout_ms: int, statement_timeout_ms: int) -> None:
        cur.execute(sql.SQL("SET lock_timeout = {}").format(sql.Literal(f"{int(lock_timeout_ms)}ms")))
        cur.execute(sql.SQL("SET statement_timeout = {}").format(sql.Literal(f"{int(statement_timeout_ms)}ms")))

    def create_index_concurrently(
        self, spec: IndexSpec, marker: dict, *, lock_timeout_ms: int, statement_timeout_ms: int
    ) -> IndexState:
        if spec.method not in ALLOWED_METHODS:
            raise ValueError(f"index method {spec.method!r} is not supported by this adapter")
        conn = self.operator
        assert conn.autocommit, "CREATE INDEX CONCURRENTLY cannot run inside a transaction block"
        with conn.cursor() as cur:
            self._session_timeouts(cur, lock_timeout_ms, statement_timeout_ms)
            cur.execute(
                sql.SQL("CREATE INDEX CONCURRENTLY {name} ON {schema}.{table} USING btree ({cols})").format(
                    name=sql.Identifier(spec.name),
                    schema=sql.Identifier(spec.schema),
                    table=sql.Identifier(spec.table),
                    cols=sql.SQL(", ").join(sql.Identifier(c) for c in spec.columns),
                )
            )
            cur.execute(
                sql.SQL("COMMENT ON INDEX {schema}.{name} IS {marker}").format(
                    schema=sql.Identifier(spec.schema),
                    name=sql.Identifier(spec.name),
                    marker=sql.Literal(json.dumps({MARKER_KEY: MARKER_VALUE, **marker}, sort_keys=True)),
                )
            )
        state = self.index_state(spec.schema, spec.name)
        if state is None:
            raise RuntimeError(f"index {spec.schema}.{spec.name} not found right after CREATE INDEX CONCURRENTLY")
        return state

    def drop_index_concurrently(self, schema: str, name: str, *, lock_timeout_ms: int) -> bool:
        conn = self.operator
        with conn.cursor() as cur:
            self._session_timeouts(cur, lock_timeout_ms, 600000)
            cur.execute(
                sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {schema}.{name}").format(
                    schema=sql.Identifier(schema), name=sql.Identifier(name)
                )
            )
        return self.index_state(schema, name) is None

    def analyze_table(self, schema: str, table: str) -> None:
        with self.operator.cursor() as cur:
            cur.execute(sql.SQL("ANALYZE {schema}.{table}").format(schema=sql.Identifier(schema), table=sql.Identifier(table)))


def _index_state(row: dict) -> IndexState:
    marker = None
    if row.get("comment"):
        try:
            parsed = json.loads(row["comment"])
            marker = parsed if isinstance(parsed, dict) and parsed.get(MARKER_KEY) == MARKER_VALUE else None
        except json.JSONDecodeError:
            marker = None
    return IndexState(
        schema=row["schema"], name=row["name"], table=row["table"], valid=bool(row["valid"]), ready=bool(row["ready"]),
        definition=row["definition"], marker=marker, size_bytes=int(row["size_bytes"] or 0),
    )
