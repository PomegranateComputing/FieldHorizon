"""
The privileged executor: the only code that holds the operator credential and sends DDL.

What it accepts is a short list of typed commands -- create one b-tree index,
drop an index this system provably created, analyze a table -- never SQL.
Each command is turned into text by templates.py from validated
identifiers; the server's own event-trigger guard refuses any operator DDL
whose text is not one of those templates, so a bug or a hostile input here
still cannot widen what the operator does.

Immediately before every action it re-verifies, itself:

* the authorization it is handed is fresh (policy max_authorization_age_seconds)
  and was issued under the lease it still holds;
* the policy file on disk still hashes to the authorized policy (policy_changed);
* the target file still describes the same target (target_changed);
* the live server is still the pinned instance (identity_mismatch);
* the operation is one the policy allows (operation_not_allowed);
* the objects are still in the state the authorization saw (object_state_changed).

Coordination is target-side, so it holds across processes with separate
local journals: a session-level advisory lock held for the whole operation,
plus a lease row whose fencing token increases at every acquisition. Every
ledger write is conditional on that token: a process that lost its lease
cannot record -- and therefore cannot act on -- anything afterwards.

The ledger row of a change is written BEFORE its DDL is sent. A drop is
only ever sent for an index whose ledger row, structure and marker (or
interrupted-build state, or recorded OID) prove it is this system's change;
an ambiguous index is refused, never guessed at.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from . import templates
from .adapters.postgres import LOCK_NAMESPACE, MARKER_KEY, MARKER_VALUE, application_name
from .model import IndexState
from .procs import current_process
from .targets import TargetConfig, TargetConfigError, load_targets, redact

SCOPE_INDEX_OPTIMIZATION = "index_optimization"

OP_CREATE_INDEX = "create_index_concurrently"
OP_DROP_OWN_INDEX = "drop_own_index_concurrently"
OP_ANALYZE = "analyze_table"
OP_VACUUM = "vacuum_table"

# Refusal codes: why the executor would not act. Each is reported verbatim to the journal and the UI.
INVALID_IDENTIFIER = "invalid_identifier"
OPERATION_NOT_ALLOWED = "operation_not_allowed"
IDENTITY_MISMATCH = "identity_mismatch"
TARGET_CHANGED = "target_changed"
POLICY_CHANGED = "policy_changed"
AUTHORIZATION_STALE = "authorization_stale"
LEASE_BUSY = "lease_busy"
LEASE_LOST = "lease_lost"
OBJECT_STATE_CHANGED = "object_state_changed"
NOT_ELIGIBLE = "not_eligible"
OWNERSHIP_UNPROVEN = "ownership_unproven"
ORPHAN_ACTIVITY = "orphan_activity"
COORDINATION_UNAVAILABLE = "coordination_unavailable"

# Execution failures: the executor acted and the server refused or interrupted it.
DDL_LOCK_TIMEOUT = "ddl_lock_timeout"
DDL_STATEMENT_TIMEOUT = "ddl_statement_timeout"
DDL_REFUSED_BY_SERVER = "ddl_refused_by_server"
DDL_FAILED = "ddl_failed"
CONNECTION_LOST = "connection_lost"


class ExecutorRefusal(Exception):
    """The executor did not act: a precondition failed. Nothing was sent for this command."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ExecutionFailed(Exception):
    """The executor acted and the action failed; ``cleaned`` tells whether the target was confirmed clean afterwards."""

    def __init__(self, code: str, detail: str, *, cleaned: bool, cleanup: dict | None = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.cleaned = cleaned
        self.cleanup = cleanup or {}


@dataclass(frozen=True)
class Lease:
    scope: str
    operation_id: str
    fencing_token: int
    backend_pid: int
    acquired_at: str

    def describe(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AuthorizationContext:
    """What was verified, for which operation, under which lease, and when. Stale after the policy's maximum age."""

    operation_id: str
    target_name: str
    target_fingerprint: str
    policy_sha256: str
    instance_marker: str | None
    system_identifier: str | None
    lease: Lease
    verified_monotonic: float
    verified_at: str
    eligibility: dict = field(default_factory=dict)

    def describe(self) -> dict:
        d = asdict(self)
        d.pop("verified_monotonic", None)
        return d


@dataclass(frozen=True)
class CreateIndex:
    change_id: str
    schema: str
    table: str
    columns: tuple[str, ...]
    index_name: str
    table_oid: int | None = None
    candidate_key: str = ""


@dataclass(frozen=True)
class DropOwnIndex:
    owner_operation_id: str
    change_id: str
    schema: str
    index_name: str
    reason: str
    recovery_ref: str | None = None


@dataclass(frozen=True)
class AnalyzeTable:
    schema: str
    table: str


@dataclass(frozen=True)
class VacuumTable:
    schema: str
    table: str


@dataclass
class OwnershipProof:
    proven: bool
    basis: str
    reasons: list[str]

    def describe(self) -> dict:
        return asdict(self)


def expected_definition(cmd: CreateIndex) -> str:
    return templates.create_index_statement(cmd.schema, cmd.table, cmd.columns, cmd.index_name).as_string(None)


def ownership_proof(state: IndexState, row: dict) -> OwnershipProof:
    """
    Is this live index the change recorded in this ledger row? Structure must
    match the template and the row; identity must come from the marker, the
    recorded OID, or an interrupted build -- and nothing may contradict it.
    """
    reasons: list[str] = []
    if (row.get("index_schema"), row.get("index_name")) != (state.schema, state.name):
        reasons.append(f"ledger names {row.get('index_schema')}.{row.get('index_name')}, live index is {state.schema}.{state.name}")
    if row.get("table_name") != state.table:
        reasons.append(f"ledger table {row.get('table_name')!r} != live table {state.table!r}")
    if list(row.get("columns") or []) != list(state.columns):
        reasons.append(f"ledger columns {list(row.get('columns') or [])} != live columns {list(state.columns)}")
    if state.method != "btree" or state.is_unique or state.has_expressions or state.is_partial:
        reasons.append("live index is not a plain b-tree on columns (not the template's shape)")
    recorded_oid = row.get("index_oid")
    if recorded_oid is not None and state.oid is not None and int(recorded_oid) != int(state.oid):
        reasons.append(f"recorded OID {recorded_oid} != live OID {state.oid}: the index was dropped and recreated by someone else")
    marker = state.marker
    basis = ""
    if marker is not None:
        if marker.get("operation_id") != row.get("operation_id") or marker.get("change_id") != row.get("change_id"):
            reasons.append(
                f"marker names {marker.get('operation_id')}/{marker.get('change_id')}, "
                f"ledger row is {row.get('operation_id')}/{row.get('change_id')}"
            )
        basis = "ledger + marker"
    elif recorded_oid is not None and state.oid is not None and int(recorded_oid) == int(state.oid):
        basis = "ledger + recorded OID (marker missing)"
    elif row.get("state") == "applying":
        basis = "ledger shows an interrupted build + matching structure (no marker yet)"
    elif row.get("state") == "reverting" and recorded_oid is None:
        # 'reverting' is only ever written after a successful proof: an interrupted drop of an interrupted build.
        basis = "ledger shows an interrupted drop of an interrupted build + matching structure"
    else:
        reasons.append(f"no marker, no recorded OID, and the ledger state {row.get('state')!r} is not an interrupted build")
    return OwnershipProof(proven=not reasons, basis=basis if not reasons else "", reasons=reasons)


def sha256_file(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


class PostgresExecutor:
    """
    One executor per operation (or recovery run). Holds one operator session:
    the session that holds the advisory lock is the session that sends the DDL,
    so a build orphaned by a client crash keeps the target reserved until the
    server finishes it.
    """

    def __init__(
        self,
        target: TargetConfig,
        policy,
        *,
        operation_id: str,
        journal_ref: str,
        targets_path: Path | None = None,
        scope: str = SCOPE_INDEX_OPTIMIZATION,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not target.operator_user:
            raise ExecutorRefusal(OPERATION_NOT_ALLOWED, f"target {target.name!r} declares no operator identity; it is read-only")
        if not target.owner_role:
            raise ExecutorRefusal(OPERATION_NOT_ALLOWED, f"target {target.name!r} declares no owner_role to SET ROLE to")
        self.target = target
        self.policy = policy
        self.operation_id = operation_id
        self.journal_ref = journal_ref
        self.targets_path = targets_path
        self.scope = scope
        self.clock = clock
        self.process = current_process()
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self.lease: Lease | None = None

    # --- session -----------------------------------------------------------
    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        params: dict[str, Any] = {
            "host": self.target.host, "port": self.target.port, "dbname": self.target.dbname,
            "user": self.target.operator_user, "sslmode": self.target.sslmode,
            "application_name": application_name("operator", self.operation_id),
            "connect_timeout": 10, "options": "-c statement_timeout=30000",
        }
        if self.target.passfile:
            params["passfile"] = self.target.passfile
        try:
            return psycopg.connect(make_conninfo(**params), autocommit=True, row_factory=dict_row)
        except psycopg.OperationalError as exc:
            raise ConnectionError(redact(str(exc)).strip()) from None

    @property
    def conn(self) -> psycopg.Connection[dict[str, Any]]:
        if self._conn is None:
            self._conn = self._connect()
        if self._conn.closed or self._conn.broken:
            # Never reconnect silently: a new session holds no lock.
            raise ExecutorRefusal(LEASE_LOST, "the operator session holding the lease is gone")
        return self._conn

    @property
    def backend_pid(self) -> int | None:
        return self.lease.backend_pid if self.lease else None

    def _one(self, query, params=None) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone() if cur.description else None

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    # --- lease -----------------------------------------------------------------
    def acquire(self) -> Lease:
        try:
            got = self._one("SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s)) AS got", (LOCK_NAMESPACE, self.scope))
        except psycopg.Error as exc:
            raise ExecutorRefusal(COORDINATION_UNAVAILABLE, redact(str(exc).strip().splitlines()[0])) from None
        if not got or not got["got"]:
            holder = None
            with contextlib.suppress(psycopg.Error):
                holder = self._one(
                    "SELECT holder_operation_id, holder_host, holder_pid, holder_backend_pid, holder_journal, "
                    "acquired_at::text, heartbeat_at::text FROM fieldhorizon_meta.operation_lease WHERE scope = %s",
                    (self.scope,),
                )
            raise ExecutorRefusal(LEASE_BUSY, f"target {self.target.name} is held by another operation: {holder}")
        try:
            row = self._one(
                "UPDATE fieldhorizon_meta.operation_lease SET fencing_token = fencing_token + 1, holder_operation_id = %s, "
                "holder_host = %s, holder_pid = %s, holder_backend_pid = pg_backend_pid(), holder_journal = %s, "
                "acquired_at = now(), heartbeat_at = now(), released_at = NULL WHERE scope = %s "
                "RETURNING fencing_token, holder_backend_pid, acquired_at::text",
                (self.operation_id, self.process["host"], self.process["pid"], self.journal_ref, self.scope),
            )
        except psycopg.Error as exc:
            self._unlock_quietly()
            raise ExecutorRefusal(COORDINATION_UNAVAILABLE, redact(str(exc).strip().splitlines()[0])) from None
        if row is None:
            self._unlock_quietly()
            raise ExecutorRefusal(COORDINATION_UNAVAILABLE, f"no lease row for scope {self.scope!r} (lab not provisioned for coordination)")
        self.lease = Lease(self.scope, self.operation_id, int(row["fencing_token"]), int(row["holder_backend_pid"]), row["acquired_at"])
        return self.lease

    def _unlock_quietly(self) -> None:
        with contextlib.suppress(psycopg.Error):
            self._one("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))", (LOCK_NAMESPACE, self.scope))

    def check_lease(self) -> None:
        """This session still holds the advisory lock and the lease row still carries our fencing token."""
        lease = self.lease
        if lease is None:
            raise ExecutorRefusal(LEASE_LOST, "no lease acquired")
        try:
            row = self._one(
                "SELECT pg_backend_pid() AS pid, "
                "EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted AND pid = pg_backend_pid() "
                "        AND objsubid = 2 AND classid = hashtext(%s)::oid AND objid = hashtext(%s)::oid) AS holds, "
                "(SELECT fencing_token FROM fieldhorizon_meta.operation_lease WHERE scope = %s) AS token, "
                "(SELECT holder_operation_id FROM fieldhorizon_meta.operation_lease WHERE scope = %s) AS holder",
                (LOCK_NAMESPACE, self.scope, self.scope, self.scope),
            )
        except psycopg.OperationalError as exc:
            raise ExecutorRefusal(LEASE_LOST, f"operator session lost: {redact(str(exc).strip().splitlines()[0])}") from None
        assert row is not None
        problems = []
        if row["pid"] != lease.backend_pid:
            problems.append(f"session changed (backend {row['pid']} != {lease.backend_pid})")
        if not row["holds"]:
            problems.append("advisory lock not held by this session")
        if row["token"] != lease.fencing_token or row["holder"] != lease.operation_id:
            problems.append(f"lease now token {row['token']} held by {row['holder']} (ours: {lease.fencing_token}, {lease.operation_id})")
        if problems:
            raise ExecutorRefusal(LEASE_LOST, "; ".join(problems))

    def heartbeat(self) -> None:
        if self.lease is None:
            return
        self._one(
            "UPDATE fieldhorizon_meta.operation_lease SET heartbeat_at = now() WHERE scope = %s AND fencing_token = %s "
            "AND holder_operation_id = %s",
            (self.scope, self.lease.fencing_token, self.lease.operation_id),
        )

    def release(self) -> None:
        if self.lease is None:
            self.close()
            return
        try:
            if self._conn is not None and not self._conn.closed and not self._conn.broken:
                self._one(
                    "UPDATE fieldhorizon_meta.operation_lease SET released_at = now(), heartbeat_at = now() "
                    "WHERE scope = %s AND fencing_token = %s AND holder_operation_id = %s",
                    (self.scope, self.lease.fencing_token, self.lease.operation_id),
                )
                self._unlock_quietly()
        except psycopg.Error:
            pass
        finally:
            self.lease = None
            self.close()

    # --- authorization ------------------------------------------------------------
    def live_identity(self) -> dict:
        row = self._one(
            "SELECT (SELECT marker::text FROM fieldhorizon_meta.instance_marker LIMIT 1) AS marker, "
            "(SELECT system_identifier::text FROM pg_control_system()) AS sysid, pg_is_in_recovery() AS in_recovery"
        )
        assert row is not None
        return row

    def authorize(self, *, eligibility: dict | None = None) -> AuthorizationContext:
        """Stamps a fresh authorization under the current lease. Call it right after the policy checks it summarises."""
        if self.lease is None:
            raise ExecutorRefusal(LEASE_LOST, "no lease acquired")
        live = self.live_identity()
        self._check_identity(live)
        return AuthorizationContext(
            operation_id=self.operation_id, target_name=self.target.name, target_fingerprint=self.target.fingerprint(),
            policy_sha256=self.policy.sha256, instance_marker=live["marker"], system_identifier=live["sysid"],
            lease=self.lease, verified_monotonic=self.clock(), verified_at=datetime.now(UTC).isoformat(),
            eligibility=eligibility or {},
        )

    def _check_identity(self, live: dict) -> None:
        if live.get("in_recovery"):
            raise ExecutorRefusal(IDENTITY_MISMATCH, "server is in recovery (not a primary)")
        if live.get("marker") != self.target.instance_marker or live.get("sysid") != self.target.system_identifier:
            raise ExecutorRefusal(
                IDENTITY_MISMATCH,
                f"live marker {live.get('marker')!r} / system identifier {live.get('sysid')!r} != pinned "
                f"{self.target.instance_marker!r} / {self.target.system_identifier!r}",
            )

    def verify_authorization(self, auth: AuthorizationContext, operation: str, *, compensation: bool = False) -> None:
        """
        Everything re-checked immediately before an action. Raises ExecutorRefusal; returns only if all hold.

        ``compensation`` (removing this system's own, proven change) keeps every
        check except the policy-file and target-file comparisons: undoing a change
        made under the policy that authorized it must stay possible after that
        policy or the target file is edited -- the live instance must still be
        the pinned one, the lease still ours, and ownership is proven separately.
        """
        if operation not in self.policy.allowed_operations:
            raise ExecutorRefusal(OPERATION_NOT_ALLOWED, f"{operation!r} is not in the policy's allowed operations")
        if auth.operation_id != self.operation_id or auth.lease != self.lease:
            raise ExecutorRefusal(AUTHORIZATION_STALE, "authorization was issued to another operation or under another lease")
        age = self.clock() - auth.verified_monotonic
        if age > self.policy.max_authorization_age_seconds:
            raise ExecutorRefusal(AUTHORIZATION_STALE, f"authorization is {age:.0f}s old; maximum {self.policy.max_authorization_age_seconds}s")
        on_disk = sha256_file(self.policy.source)
        if compensation:
            pass
        elif on_disk != auth.policy_sha256 or auth.policy_sha256 != self.policy.sha256:
            raise ExecutorRefusal(POLICY_CHANGED, f"policy file {self.policy.source} now hashes to {on_disk}, authorized {auth.policy_sha256}")
        if self.targets_path is not None and not compensation:
            try:
                current = load_targets(self.targets_path).get(self.target.name)
            except TargetConfigError as exc:
                raise ExecutorRefusal(TARGET_CHANGED, f"target file unreadable: {exc}") from None
            if current is None or current.fingerprint() != auth.target_fingerprint:
                raise ExecutorRefusal(TARGET_CHANGED, f"target {self.target.name!r} changed in {self.targets_path} since authorization")
        if auth.target_fingerprint != self.target.fingerprint():
            raise ExecutorRefusal(TARGET_CHANGED, "authorization was issued for another target configuration")
        self.check_lease()
        live = self.live_identity()
        self._check_identity(live)
        if (live.get("marker"), live.get("sysid")) != (auth.instance_marker, auth.system_identifier):
            raise ExecutorRefusal(IDENTITY_MISMATCH, "live instance differs from the one authorized")

    # --- ledger (fenced) ----------------------------------------------------------
    def _fenced(self, statement: str, params: tuple) -> int:
        assert self.lease is not None
        fence = (
            " AND EXISTS (SELECT 1 FROM fieldhorizon_meta.operation_lease WHERE scope = %s AND fencing_token = %s "
            "AND holder_operation_id = %s AND released_at IS NULL)"
        )
        with self.conn.cursor() as cur:
            cur.execute(statement + fence, (*params, self.scope, self.lease.fencing_token, self.lease.operation_id))
            return cur.rowcount

    def ledger_row(self, operation_id: str, change_id: str) -> dict | None:
        return self._one(
            "SELECT operation_id, change_id, fencing_token, action, index_schema, index_name, table_name, columns, definition, "
            "index_oid, state, holder_host, holder_journal, recovered_by, note FROM fieldhorizon_meta.change_ledger "
            "WHERE operation_id = %s AND change_id = %s",
            (operation_id, change_id),
        )

    def _set_ledger(self, operation_id: str, change_id: str, state: str, *, note: str | None = None,
                    index_oid: int | None = None, definition: str | None = None, recovered_by: str | None = None) -> None:
        n = self._fenced(
            "UPDATE fieldhorizon_meta.change_ledger SET state = %s, note = COALESCE(%s, note), "
            "index_oid = COALESCE(%s, index_oid), definition = COALESCE(%s, definition), "
            "recovered_by = COALESCE(%s, recovered_by), updated_at = now() WHERE operation_id = %s AND change_id = %s",
            (state, note, index_oid, definition, recovered_by, operation_id, change_id),
        )
        if n != 1:
            raise ExecutorRefusal(LEASE_LOST, f"ledger update {operation_id}/{change_id} -> {state} rejected by the fencing check")

    # --- catalog (operator session, read-only queries) -------------------------------
    def _index_state(self, schema: str, name: str) -> IndexState | None:
        from .adapters.postgres import _INDEX_STATE_SELECT, _index_state

        row = self._one(_INDEX_STATE_SELECT + " WHERE n.nspname = %s AND i.relname = %s", (schema, name))
        return _index_state(row) if row else None

    def _relation_exists(self, schema: str, name: str) -> bool:
        return self._one(
            "SELECT 1 AS x FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s",
            (schema, name),
        ) is not None

    # --- commands --------------------------------------------------------------------
    def create_index(self, auth: AuthorizationContext, cmd: CreateIndex) -> IndexState:
        try:
            statement = templates.create_index_statement(cmd.schema, cmd.table, cmd.columns, cmd.index_name)
        except templates.InvalidIdentifier as exc:
            raise ExecutorRefusal(INVALID_IDENTIFIER, str(exc)) from None
        self.verify_authorization(auth, OP_CREATE_INDEX)
        if auth.eligibility.get("candidate_key") and auth.eligibility["candidate_key"] != cmd.candidate_key:
            raise ExecutorRefusal(AUTHORIZATION_STALE, f"authorization covers {auth.eligibility['candidate_key']}, not {cmd.candidate_key}")
        table = self._one(
            "SELECT c.oid::bigint AS oid, c.relkind::text AS kind, c.relowner::regrole::text AS owner, "
            "pg_has_role(current_user, c.relowner, 'SET') AS can_set "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s",
            (cmd.schema, cmd.table),
        )
        if table is None or table["kind"] != "r":
            raise ExecutorRefusal(OBJECT_STATE_CHANGED, f"{cmd.schema}.{cmd.table} is no longer an ordinary table")
        if cmd.table_oid is not None and int(table["oid"]) != int(cmd.table_oid):
            raise ExecutorRefusal(OBJECT_STATE_CHANGED, f"{cmd.schema}.{cmd.table} was recreated since it was evaluated (OID changed)")
        if table["owner"] != self.target.owner_role or not table["can_set"]:
            raise ExecutorRefusal(NOT_ELIGIBLE, f"{cmd.schema}.{cmd.table} is owned by {table['owner']}, outside the operator's reach")
        columns = {r["attname"] for r in self._rows(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped", (table["oid"],))}
        missing = [c for c in cmd.columns if c not in columns]
        if missing:
            raise ExecutorRefusal(OBJECT_STATE_CHANGED, f"columns {missing} no longer exist on {cmd.schema}.{cmd.table}")
        if self._relation_exists(cmd.schema, cmd.index_name):
            raise ExecutorRefusal(OBJECT_STATE_CHANGED, f"a relation named {cmd.schema}.{cmd.index_name} already exists")

        definition = expected_definition(cmd)
        inserted = self._fenced(
            "INSERT INTO fieldhorizon_meta.change_ledger (operation_id, change_id, fencing_token, action, index_schema, index_name, "
            "table_name, columns, definition, state, holder_host, holder_journal) "
            "SELECT %s, %s, %s, 'create_index', %s, %s, %s, %s, %s, 'applying', %s, %s WHERE true",
            (self.operation_id, cmd.change_id, auth.lease.fencing_token, cmd.schema, cmd.index_name, cmd.table,
             list(cmd.columns), definition, self.process["host"], self.journal_ref),
        )
        if inserted != 1:
            raise ExecutorRefusal(LEASE_LOST, "ledger intent rejected by the fencing check")

        marker = json.dumps({MARKER_KEY: MARKER_VALUE, "operation_id": self.operation_id, "change_id": cmd.change_id,
                             "fencing_token": auth.lease.fencing_token, "candidate": cmd.candidate_key}, sort_keys=True)
        try:
            self._as_owner([
                (sql.SQL("SET lock_timeout = {}").format(sql.Literal(f"{int(self.policy.lock_timeout_ms)}ms")), None),
                (sql.SQL("SET statement_timeout = {}").format(sql.Literal(f"{int(self.policy.statement_timeout_ms)}ms")), None),
                (statement, None),
                (templates.comment_index_statement(cmd.schema, cmd.index_name, marker), None),
            ])
        except psycopg.Error as exc:
            raise self._failed_build(cmd, exc) from None
        state = self._index_state(cmd.schema, cmd.index_name)
        if state is None or not state.valid:
            raise ExecutionFailed(DDL_FAILED, f"{cmd.schema}.{cmd.index_name} missing or invalid right after its build", cleaned=False)
        self._set_ledger(self.operation_id, cmd.change_id, "applied", index_oid=state.oid, definition=state.definition)
        return state

    def _rows(self, query, params=None) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return list(cur.fetchall()) if cur.description else []

    def _as_owner(self, statements: list[tuple[Any, Any]]) -> None:
        """Runs template statements under SET ROLE to the owner, always returning to the operator's own role."""
        assert self.target.owner_role is not None
        conn = self.conn
        with conn.cursor() as cur:
            cur.execute(templates.set_role_statement(self.target.owner_role))
            try:
                for statement, params in statements:
                    cur.execute(statement, params)
            finally:
                if not conn.broken and not conn.closed:
                    try:
                        cur.execute("RESET ROLE")
                        cur.execute("RESET lock_timeout")
                        cur.execute("RESET statement_timeout")
                    except psycopg.Error:
                        pass

    def _classify(self, exc: psycopg.Error) -> str:
        if isinstance(exc, psycopg.errors.LockNotAvailable):
            return DDL_LOCK_TIMEOUT
        if isinstance(exc, psycopg.errors.QueryCanceled):
            return DDL_STATEMENT_TIMEOUT
        if isinstance(exc, psycopg.errors.InsufficientPrivilege):
            return DDL_REFUSED_BY_SERVER
        if isinstance(exc, psycopg.OperationalError) or self._conn is None or self._conn.broken:
            return CONNECTION_LOST
        return DDL_FAILED

    def _failed_build(self, cmd: CreateIndex, exc: psycopg.Error) -> ExecutionFailed:
        """A build failed: remove what it left behind if (and only if) it is provably ours, and say whether that worked."""
        code = self._classify(exc)
        detail = redact(str(exc).strip().splitlines()[0]) if str(exc).strip() else type(exc).__name__
        if code == CONNECTION_LOST:
            return ExecutionFailed(code, detail + " -- the server may still be building; recovery will check", cleaned=False)
        try:
            state = self._index_state(cmd.schema, cmd.index_name)
            if state is None:
                self._set_ledger(self.operation_id, cmd.change_id, "reverted", note=f"build failed before any index existed: {code}")
                return ExecutionFailed(code, detail, cleaned=True, cleanup={"index_present": False})
            row = self.ledger_row(self.operation_id, cmd.change_id)
            proof = ownership_proof(state, row or {})
            if not proof.proven:
                return ExecutionFailed(code, detail, cleaned=False, cleanup={"index_present": True, "proof": proof.describe()})
            self._set_ledger(self.operation_id, cmd.change_id, "reverting", note=f"build failed ({code}); removing its leftover")
            self._as_owner([
                (sql.SQL("SET lock_timeout = {}").format(sql.Literal(f"{int(self.policy.lock_timeout_ms)}ms")), None),
                (templates.drop_index_statement(cmd.schema, cmd.index_name), None),
            ])
            if self._index_state(cmd.schema, cmd.index_name) is None:
                self._set_ledger(self.operation_id, cmd.change_id, "reverted", note=f"build failed ({code}); leftover removed")
                return ExecutionFailed(code, detail, cleaned=True,
                                       cleanup={"index_present": True, "was_valid": state.valid, "dropped": True, "proof": proof.describe()})
            return ExecutionFailed(code, detail, cleaned=False, cleanup={"index_present": True, "dropped": False})
        except (psycopg.Error, ExecutorRefusal) as cleanup_exc:
            return ExecutionFailed(code, detail, cleaned=False,
                                   cleanup={"error": redact(str(cleanup_exc).strip().splitlines()[0])})

    def drop_own_index(self, auth: AuthorizationContext, cmd: DropOwnIndex, *, compensation: bool = True) -> dict:
        try:
            statement = templates.drop_index_statement(cmd.schema, cmd.index_name)
        except templates.InvalidIdentifier as exc:
            raise ExecutorRefusal(INVALID_IDENTIFIER, str(exc)) from None
        self.verify_authorization(auth, OP_DROP_OWN_INDEX, compensation=compensation)
        row = self.ledger_row(cmd.owner_operation_id, cmd.change_id)
        if row is None:
            raise ExecutorRefusal(OWNERSHIP_UNPROVEN, f"no ledger row {cmd.owner_operation_id}/{cmd.change_id}: never dropped by name alone")
        if (row["index_schema"], row["index_name"]) != (cmd.schema, cmd.index_name):
            raise ExecutorRefusal(OWNERSHIP_UNPROVEN, f"ledger row names {row['index_schema']}.{row['index_name']}, not {cmd.schema}.{cmd.index_name}")
        if row["state"] == "reverted":
            return {"dropped": False, "already_reverted": True, "ledger": row}
        if row["state"] == "retained":
            raise ExecutorRefusal(NOT_ELIGIBLE, f"{cmd.schema}.{cmd.index_name} is a retained change; compensation never removes one")
        state = self._index_state(cmd.schema, cmd.index_name)
        if state is None:
            self._set_ledger(cmd.owner_operation_id, cmd.change_id, "reverted", note=f"absent when compensating: {cmd.reason}",
                             recovered_by=cmd.recovery_ref)
            return {"dropped": False, "absent": True, "ledger": row}
        proof = ownership_proof(state, row)
        if not proof.proven:
            raise ExecutorRefusal(OWNERSHIP_UNPROVEN, "; ".join(proof.reasons))
        self._set_ledger(cmd.owner_operation_id, cmd.change_id, "reverting", note=cmd.reason, recovered_by=cmd.recovery_ref)
        try:
            self._as_owner([
                (sql.SQL("SET lock_timeout = {}").format(sql.Literal(f"{int(self.policy.lock_timeout_ms)}ms")), None),
                (statement, None),
            ])
        except psycopg.Error as exc:
            code = self._classify(exc)
            raise ExecutionFailed(code, redact(str(exc).strip().splitlines()[0]), cleaned=False,
                                  cleanup={"index_present": True, "proof": proof.describe()}) from None
        if self._index_state(cmd.schema, cmd.index_name) is not None:
            raise ExecutionFailed(DDL_FAILED, f"{cmd.schema}.{cmd.index_name} still present after DROP INDEX CONCURRENTLY", cleaned=False)
        self._set_ledger(cmd.owner_operation_id, cmd.change_id, "reverted", note=cmd.reason, recovered_by=cmd.recovery_ref)
        return {"dropped": True, "was_valid": state.valid, "had_marker": state.marker is not None, "proof": proof.describe(),
                "index": state.describe()}

    def mark_retained(self, auth: AuthorizationContext, change_id: str) -> None:
        self.verify_authorization(auth, OP_CREATE_INDEX)
        row = self.ledger_row(self.operation_id, change_id)
        if row is None or row["state"] != "applied":
            raise ExecutorRefusal(OBJECT_STATE_CHANGED, f"ledger row {self.operation_id}/{change_id} is not 'applied': {row}")
        self._set_ledger(self.operation_id, change_id, "retained", note="every acceptance criterion passed")

    def analyze_table(self, auth: AuthorizationContext, cmd: AnalyzeTable) -> None:
        try:
            statement = templates.analyze_statement(cmd.schema, cmd.table)
        except templates.InvalidIdentifier as exc:
            raise ExecutorRefusal(INVALID_IDENTIFIER, str(exc)) from None
        self.verify_authorization(auth, OP_ANALYZE)
        self._as_owner([(statement, None)])

    def vacuum_table(self, auth: AuthorizationContext, cmd: VacuumTable) -> None:
        """Removes the dead tuples measurement writes leave behind, so every phase starts from the same state."""
        try:
            statement = templates.vacuum_statement(cmd.schema, cmd.table)
        except templates.InvalidIdentifier as exc:
            raise ExecutorRefusal(INVALID_IDENTIFIER, str(exc)) from None
        self.verify_authorization(auth, OP_VACUUM)
        self._as_owner([(statement, None)])
