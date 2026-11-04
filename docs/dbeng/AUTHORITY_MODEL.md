# Field Horizon DB engineering — authority model (DB-02)

Written for: whoever has to decide whether to trust an autonomous change on a PostgreSQL target — an
operator, a DBA reviewing the lab, or a security reviewer. Every statement below is either enforced
by the server, enforced by code with a named test, or explicitly marked as *not* enforced.

> **Status: experimental.** DB-02 has been validated only against an isolated PostgreSQL 18.6 test
> instance holding a synthetic lab, on a single Windows workstation. It is **not** ready for
> autonomous operation in production and must not be pointed at a production database. Open limits:
> no separate Windows identity for the executor (§5), display scaling validated only by browser
> emulation, pre-existing mypy errors in test files outside DB-02 (stated as 124, recounted as 118:
> 92 inherited from `main`, 26 from DB-01), no Windows installer (`REPORT_DB02.md`).

## 1. Who can do what

| Identity | Connects as | Can | Cannot | Enforced by |
|---|---|---|---|---|
| **Observer** | `fh_observer` | read catalogs, statistics (`pg_read_all_stats`), plans of record, the coordination tables, `pg_control_system()`, `bt_index_check` | write anything (`default_transaction_read_only = on` at role level and per session) | PostgreSQL role settings and grants |
| **Measurement** | `fh_measure` | `SELECT` on `bench.customers` and `bench.orders`, `INSERT` on `bench.orders` (always inside a transaction the measurement rolls back) | `UPDATE`, `DELETE`, `TRUNCATE`, any DDL, own anything | grants only; it owns nothing (`test_03`) |
| **Operator** (executor only) | `fh_operator` | `SET ROLE fh_bench_owner` (membership **WITH INHERIT FALSE, SET TRUE**); then only the executor templates: `CREATE INDEX CONCURRENTLY "fhx_…" … USING btree (cols)`, `DROP INDEX CONCURRENTLY IF EXISTS "…"."fhx_…"`, `COMMENT ON INDEX "…"."fhx_…" IS '…'`; `ANALYZE` and `VACUUM` of a lab table; writes to `fieldhorizon_meta.operation_lease` and `change_ledger` | any other DDL (event trigger `fieldhorizon_guard_operator` on `ddl_command_start`, before anything is created); any `INSERT/UPDATE/DELETE/TRUNCATE` on a lab table, even as owner (statement triggers); anything on `bench.audit_log` (owned by `fh_protected_owner`, not reachable) | PostgreSQL: role membership options, the read-only `SECURITY DEFINER` guard, DML statement triggers, table ownership (`test_03`, `test_04`, `C02` smoke) |
| **Advisor / LLM agents** | — | read the numbered evidence pack; propose candidates | hold any credential or connection; decide anything | structural: they receive data only (`agents.py`), their proposals go through the same policy gate (`test_llm_agents_inform_but_never_decide…`) |

The administrator role (`fh_admin`, superuser of the isolated test instance) is used only by
`bench-setup` to build the lab. No Field Horizon runtime path connects as it.

## 2. What the executor checks before every action

`fieldhorizon/dbeng/executor.py` is the only module that opens an operator session. It accepts
typed commands (`CreateIndex`, `DropOwnIndex`, `AnalyzeTable`, `VacuumTable`), never SQL text, and
immediately before acting it verifies — itself, on its own session:

1. identifiers match `templates.py` (lower-case, ≤ 63 bytes, `fhx_` names): `invalid_identifier`;
2. the operation is in the policy's `allowed_operations`: `operation_not_allowed`;
3. the authorization was issued to this operation, under this lease, less than
   `max_authorization_age_seconds` ago (300 s): `authorization_stale`;
4. the policy file on disk still hashes to the authorized policy: `policy_changed`;
5. the target file still describes the same target (fingerprint of the whole declaration): `target_changed`;
6. this session still holds the advisory lock and the lease row still carries its fencing token: `lease_lost`;
7. the live server is still the pinned instance (instance marker and `system_identifier`), and a primary: `identity_mismatch`;
8. the objects are as evaluated: the table exists with the same OID and owner, the columns exist, no
   relation already has the index name: `object_state_changed` / `not_eligible`.

The operation, separately and just before, re-runs the write authority and the candidate's
eligibility on fresh facts (including open transactions) and stamps the authorization the executor
then checks (`authorize` step).

**Compensation** (removing this system's own proven change) keeps checks 1–3 and 6–7 and the
ownership proof, but not 4–5: undoing a change made under the policy that authorized it must remain
possible after that policy or the target file is edited. It is recorded as such.

## 3. Coordination across processes

* A session-level advisory lock `pg_try_advisory_lock(hashtext('fieldhorizon.dbeng'), hashtext(scope))`
  held by the executor session for the whole operation; the server releases it when that session
  ends — including when the process is killed.
* A lease row (`fieldhorizon_meta.operation_lease`) whose **fencing token increases at every
  acquisition**, with the holder's operation, host, PID, backend PID and journal.
* Every ledger write is conditional on that token (`… AND EXISTS (lease row with this token and
  holder)`): a process that lost its lease cannot record anything afterwards.
* The session that holds the lock is the session that sends the DDL, so a build orphaned by a client
  crash keeps the target reserved until the server has finished it.

Tested with two real CLI processes and two separate journals (`test_08`), with an exclusive lease that
dies with its session and comes back with token + 1 (`test_13`), and with five concurrent launches of
one request id in one service (`test_15`).

## 4. Proving a change is ours before removing it

Nothing is ever dropped by name or prefix. `executor.ownership_proof` requires:

* a ledger row for exactly this operation and change, naming this schema and index;
* the live index to have the template's structure and the ledger's table and columns (plain b-tree,
  not unique, no expression, no predicate);
* and identity from one of: the marker comment names this operation and change; or the recorded OID
  equals the live OID; or the ledger shows an interrupted build (`applying`, or `reverting` with no
  OID recorded — `reverting` is only ever written after a successful proof).

Any contradiction blocks: a marker naming another change, a recorded OID that differs (the index was
dropped and recreated by someone else), a different structure. An index named `fhx_…` that no ledger
knows is reported as `unledgered_index` and never touched (`test_recovery_never_drops_what_it_cannot_prove`).

## 5. Residual power — stated, not hidden

* **Owner powers no trigger can intercept.** Under `SET ROLE fh_bench_owner` the operator can still
  `SELECT` the lab data, `LOCK TABLE`, `VACUUM`, `ANALYZE`, `CLUSTER` (measured in DB-02 probe
  `B03`). It can also create, comment and drop template-conformant `fhx_` indexes on the lab tables —
  including writing a marker comment of its choosing, which is why the proof never rests on the marker
  alone.
* **Who can reach the operator credential.** It lives in the libpq password file
  `%LOCALAPPDATA%\FieldHorizon\dbeng\pgpass.conf` (ACL: owner only), readable by **any process
  running as the Windows user running the service** — Field Horizon's service, its CLI, and
  anything else that user runs. The module boundary between advisor and executor is structural (no
  code path gives the advisor a connection), not a security boundary.
* **Unmet condition: a separate Windows identity for the executor.** True isolation would run the
  executor under its own Windows account (or a service account) holding the only copy of the operator
  secret. That requires creating an account and a service, i.e. administrator rights, which this
  workstation's user does not have (member of Administrators "for deny only", medium integrity).
  It was therefore not done. Everything else in this document holds regardless; this one condition
  is declared **not satisfied**.

## 6. What the operator interface can and cannot do

The interface posts typed requests only: a launch (target, workload file name, optional extra
candidates in `schema.table(col)` form, a request id), a control (`pause`, `resume`, `cancel`), a
manual recovery. There is no SQL editor and no switch that disables a protection. A confirmation
dialog in the interface confirms an intent; the backend checks it again.

## 7. Where each claim is tested

| Claim | Test |
|---|---|
| Refusal of arbitrary SQL, out-of-scope DDL, `TRUNCATE`, DML, protected table — no residue | `tests/test_dbeng_postgres.py::test_03` (real server) |
| Malicious identifiers, stale authorization, policy change, target change, lease lost, identity mismatch, drop by name alone | `test_04` (real server); `test_dbeng_operation.py` (policy change before and between authorize/apply, stale authorization) |
| Two processes, separate journals, one target | `test_08` |
| Kill during the build; the server completes it; recovery proves and removes it | `test_09` |
| Crash after the build; automatic start-up recovery; idempotent | `test_10` |
| Long transaction opened after the precheck; lock timeout; recovery after it ends | `test_11` |
| Statement timeout during the build | `test_12` |
| Lease exclusive, dies with its session, new fencing token | `test_13` |
