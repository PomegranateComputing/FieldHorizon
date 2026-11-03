# Operation Autonomous DB-01 — technical report

Windows 11 Home (build 26300), Ryzen 9 7900, 31 GB RAM, RTX 5070 Ti; Python 3.12.10, Node 24.22.2,
Ollama 0.40.13 (`hermes3:8b`, `nomic-embed-text`), PostgreSQL 18.6. Branch `windows-baseline-db01`
(16 commits on top of `7a4fd60`, not pushed). Run on 3 November 2026.

**Verdict.** The Windows baseline is repaired and verified. A PostgreSQL engineering capability now
exists, and the autonomous index-optimization loop ran end to end against a real PostgreSQL engine:
detect, design, validate, build, measure, keep or revert, recover. No human approved any
intermediate step. All nine Phase III acceptance criteria are met by recorded evidence.

The scope is narrow on purpose: one operation family (single-table b-tree indexes), on an isolated
test instance only.

Local evidence is under `_qualification/db01/` (gitignored). Every operation's files are registered
with their SHA-256 in the journal, and `field-horizon db evidence <id>` re-verifies them.

## 1. Phase I — Windows baseline

| Item | Finding | Fix (commit) | Proof |
|---|---|---|---|
| 10 pre-existing uncommitted adaptations | Audited one by one against a detached `HEAD` worktree. On `HEAD`: 43 failures (39 YAML-escape fixture failures across 7 files, 3 `os.uname` RunLock failures, 1 chmod on NTFS), and Rust parity skipped because `fh-core.exe` was never found. The message of `b440446` says "37"; the audit log (`01_audit_head.log`) shows 39 | Kept byte-identical, then committed in scoped commits (`4b83404`, `1880552`, `b440446`) | Same tests: 63 passed, 1 justified skip |
| `data/sources.yaml` | Unterminated quote: `load_manifest` raised `ScannerError` | `90814d2` | Manifest loads |
| Vite asset base | `/ui` served a blank page (`/assets/*.js` returned 404) | `base: "./"` (`518b6b5`) | UI loads in Edge 156 |
| Rust core discovery | Resolved under the config directory, so any isolated config silently degraded synthesis | Env override, then a crate vendored next to the config, then the package crate (`bf9f4fd`) | Real cycle: `RustPressureComputed` with `status: ok`, `resolved_via: package`, binary sha256 `e32e3d4a…` (matches the file), 173 edges. In that cycle 0 of them touched the retrieved axioms: the core ran but contributed nothing to that prompt |
| Export evaluation discrepancy | The bundle "recomputed" 0.919 CANON for a cycle persisted as 0.819 USEFUL_FRAGMENT. Root cause: the recomputation scored the fragment without the cycle's 6 axioms, so enforcement rose from 0.76 to 1.0 (vacuous) and stuffing fell from 0.082 to 0. Re-scored in its original context it gives exactly 0.819 | `cycle_evaluations` (schema 7, additive): original components, evaluator version and context, written in the cycle's transaction. `GET /cycles/{id}/evaluation` and the `cycle-evaluation` CLI keep the persisted decision, the original components and a labelled recomputation apart; pre-v7 cycles report "unavailable" (`3293154`) | Real cycle: recorded 0.846 equals recomputed 0.846, same context hash, no deltas. 8 tests |
| Tauri API origin | The native app called `http://tauri.localhost/health` and received HTML | API base `http://127.0.0.1:<port>` in the webview only; CORS allows exactly `http://tauri.localhost` and `tauri://localhost`; token still required (`9eca865`) | Release `desktop.exe`: `/health`, `/capabilities`, `/status`, `/sources` and the `/events/stream` SSE all go to 127.0.0.1:8777 with 200; the CORPUS page renders. No Windows installer target exists (not required here) |
| Contract export | CRLF written on Windows | LF on every OS (`1a5a09f`) | No contract drift |

**Final baseline (12/12 green).**

- pytest: **1377 passed**, 1 skipped (chmod on NTFS). This includes the documented `vec` extra (sqlite-vec loaded; `vec0` tables populated during the real cycle) and 6 tests against the real PostgreSQL instance.
- ruff and mypy: clean.
- Rust core: build OK, and Python/Rust parity OK. The `fieldhorizon-rust` crate has no Rust unit tests of its own (0 run); its correctness rests on the parity tests.
- Frontend: tsc OK, vitest 54/54, production build OK.
- Contract: no drift.
- Tauri crate: `cargo check` OK, `cargo test` 6/6.

Bounded real integration on an isolated database: one multi-agent cycle with `hermes3:8b`, through
the browser UI.

## 2. Phase II — what was built

`fieldhorizon/dbeng/` is a separate package. It reuses the Ollama client, domain events, provenance
edges and the API/UI shell. Its state lives in additive `dbeng_*` tables; no target business data
enters Field Horizon.

- **Targets and secrets.** Targets carry no secret, and a target file with credential-like keys is refused. Passwords live only in a libpq password file with an owner-only ACL. The bench setup sends SCRAM verifiers, never plaintext; checked that no password statement reaches `pg_stat_statements`.
- **Least-privilege identities.**
  - `fh_observer` is read-only by role default and by session, with `pg_read_all_stats` and EXECUTE on `pg_control_system()` and amcheck.
  - `fh_operator` is a member of the table-owner role, because PostgreSQL has no CREATE INDEX privilege apart from ownership. An event trigger limits it to `CREATE INDEX`, `DROP INDEX` and `COMMENT`; probed: DROP/CREATE/ALTER TABLE are refused.
- **Adapter.** An engine-neutral protocol with a psycopg implementation.
  - Discovery covers schemas, tables, columns, indexes, constraints, view dependencies, table and column statistics, and `pg_stat_statements`. It gives explicit `available`/`unavailable`/`unsupported`/`unverified` statuses; for example hypopg is unavailable.
  - Plans come from `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, VERBOSE, FORMAT JSON)`. Writes are measured inside force-rolled-back transactions.
  - Snapshots are versioned and hashed.
- **Advisor.** Deterministic. It proposes candidates from measured selective sequential scans on plain-column predicates, and reports structural findings such as a foreign key without an index.
- **Policy** (`config/dbeng_policy.yaml`, hashed into every operation) is the only authority.
  - *Authority:* writes need an `isolated_test` target whose instance marker and `system_identifier` the live server proves.
  - *Eligibility:* 16 checks covering schema, protection, columns, duplication, size, selectivity, storage, dependencies, privilege and long transactions. **Unverifiable means blocked.**
  - *Retention:* an A/A-bounded baseline noise; a target median at least 30% faster with p < 0.01 and a bootstrap CI below 1; the new index used in at least 90% of plans; no significant read regression above 10% or write regression above 30%; and the weighted workload at least 5% better.
- **Agents.** Five LLM roles, advisory only.
  - Uncited claims are discarded.
  - Proposals pass the same gate as any candidate.
  - Objections are mapped to the checks that test them.
  - The validator's agreement is recorded and never used.
- **Operation.** A journaled state machine: steps, a change ledger with undo, 10 event types, provenance edges, and hashed evidence with a Markdown report. Failures end as `FAILED_RECOVERED` or `FAILED_NEEDS_RECOVERY`; `db recover` is idempotent and never drops an index it cannot prove is its own.
- **Interfaces.**
  - CLI: `field-horizon db targets | discover | optimize-indexes | operations | operation | recover | evidence | bench-setup | bench-verify`.
  - FastAPI `/db/*`: 9 routes; the contract is purely additive (14 schemas, 0 changed).
  - UI: the DATABASE page.
- **Tests.**
  - Unit tests: statistics, policy, advisor, targets, agents and the CLI, without a database.
  - The state machine on an in-memory adapter: every terminal path plus crash recovery.
  - API tests.
  - 6 acceptance/rejection tests against the real PostgreSQL.

## 3. Phase III — real verification (PostgreSQL 18.6, 2 000 000 orders)

| Run | Operation | Outcome |
|---|---|---|
| R1 discovery | snapshot `S-20261103T154031-bde57994` | Identity proven. Findings: `orders.customer_id` foreign key without an index; large tables scanned sequentially |
| R2 autonomous loop + LLM agents | `OP-20261103T154239-cd1ada` | **RETAINED** `bench.orders(customer_id)` in 20 s, with no intervention |
| R3 same operation again | `OP-20261103T154325-b23934` | NO_CANDIDATE (idempotent) |
| R4 poor/prohibited candidates | `OP-20261103T154330-dd190e` | **REJECTED**, nothing written. `orders(status)`: one value matches 20.3% of rows (limit 5%). `audit_log(event_type)`: protected table. `pg_catalog.pg_class(relname)`: forbidden schema, already indexed, no privilege |
| R5 write-dominated workload | `OP-20261103T154336-078f07` | Index built (118 MB, 1.5 s), measured, **REVERTED**; catalog verified |
| R6 crash after apply + restarts | `OP-20261103T154420-8af4b4` | Hard exit 86 left a valid index with no decision. A new operation was BLOCKED. `recover` gave RECOVERED_REVERTED; a second `recover` had nothing to do. amcheck passed on 4 b-trees with 0 invalid indexes; row counts unchanged |
| R7 real kill during CREATE INDEX CONCURRENTLY | `OP-20261103T160023-857ca4` | At t+0 the index was INVALID and unmarked. At t+2 s it was **VALID and unmarked**: the server finished the build for the dead client. Recovered through the ledger after a restart; amcheck consistent |
| R8 authority | `OP-…160056-407007`, `OP-…160057-380d10` | **BLOCKED**: the same instance declared `production`; a wrong instance marker. No write of any kind |
| R9 operator UI | `OP-20261103T160133-c08c9c` | Launched from the DATABASE page in Edge; live progress; REVERTED; evidence zip with 11 files, all sha256 verified |

**Acceptance criteria.**

1. A relevant candidate was identified (R2: the plan read 2 000 000 rows to return 7).
2. The policy gate validated it (16/16 checks).
3. The index was created (`fhx_orders_customer_id_640a0cb9`, valid, amcheck OK).
4. The workload was measured before and after (108 samples each).
5. The decision followed the measurements (R2 RETAIN, R5 REVERT).
6. Poor and prohibited candidates were rejected (R4, plus an agent-hallucinated `audit_log(customer_id)` in R2).
7. A regression triggered a rollback (R5, R9).
8. The database stayed consistent after interruption and restart (R6, R7, plus an unplanned postmaster kill with WAL recovery).
9. Every reported result maps to hashed evidence (re-verified: 14 + 11 + 11 files).

## 4. Before/after measurements

Server execution time in ms (EXPLAIN ANALYZE), median [p25–p75], n per arm. The ratio is
after/before, with a seeded bootstrap 95% CI; p is the one-sided Mann-Whitney test.

**R2 — retained** (`OP-20261103T154239-cd1ada`). decision.json `6303019c…`, baseline.json `e148fb91…`, after.json `2599dda0…`.

| Query (kind, weight) | Before | After | Ratio [95% CI] | p | Criterion |
|---|---|---|---|---|---|
| orders_by_customer (read, 50) | 71.121 [70.249–71.629] n=32 | 0.025 [0.023–0.028] n=32 | 0.0003 [0.0003, 0.0004] | faster 3.2e-12 | target: pass; index in 100% of plans |
| customers_by_region (read, 5) | 7.449 [7.306–7.534] | 7.572 [7.305–7.769] | 1.017 [0.991, 1.036] | slower 0.035 (n.s.) | pass |
| orders_status_summary (read, 1) | 263.458 [261.779–265.438] n=12 | 263.723 [262.838–265.135] | 1.001 [0.992, 1.011] | slower 0.31 | pass |
| order_insert_batch (write, 2) | 5.401 [5.309–5.589] | 6.236 [6.005–6.457] | 1.155 [1.117, 1.185] | slower 4.7e-12 | pass (index upkeep, within 30%) |

- Weighted workload: 3867.6 → 315.3 (**×0.082**).
- A/A baseline noise: 1.5% on the target query.
- The index is 18.6 MB and was built in 0.74 s.

**R5 — reverted** (`OP-20261103T154336-078f07`). decision.json `4e85e6c4…`.

| Query (kind, weight) | Before | After | Ratio [95% CI] | p | Criterion |
|---|---|---|---|---|---|
| order_by_note (read, 1) | 86.002 | 0.012 | 0.0001 [0.0001, 0.0002] | faster 1.4e-09 | target: pass |
| order_insert_batch (write, 400) | 23.421 [22.739–23.934] | 39.591 [35.527–41.229] | 1.690 [1.573, 1.751] | slower 3.3e-12 | **fail** |
| orders_status_summary (read, 1) | 252.211 | 262.938 | 1.043 [1.016, 1.070] | slower 2.4e-04 | pass (< 10%) |

Weighted workload: 9706.6 → 16 099.1 (**×1.659**), so `weighted_workload_improved` **failed** and
the index was dropped.

**Agents in R2 (`hermes3:8b`, advisory).**

- All 5 roles returned cited JSON, and the validator agreed with the policy.
- The planner also proposed a non-existent `bench.audit_log(customer_id)`. The gate rejected it: protected table, missing column.
- The reviewer raised a wrong "low selectivity" objection. It was tested by `leading_column_selective` (pass) and recorded as context only.

## 5. Incidents and honest notes

- **PostgreSQL binaries are unsigned.** The official EDB zip contains no Authenticode signature. Integrity rests on HTTPS and the recorded SHA-256. Only `bin`, `lib`, `share` and one documentation page were extracted (145 MB).
- **uv downloaded Python 3.13** (because of `.python-version`) just to resolve `uv.lock`. It was uninstalled; use `--python 3.12`.
- **First R2 run** (`…154039-788891`): two roles' output was lost to unquoted ids (`[E25]`), and the planner cited an internal ref that the evidence pack had itself exposed. Fixed in `4c716d1`; the bench was rebuilt and R2 rerun. The first run's decision was unaffected and remains in the journal.
- **A fresh Field Horizon journal broke the first event:** the db commands did not run `init_db`. Fixed in `6d09323`, with a test that fails without the fix.
- **Test-instance incident.**
  - A `pg_ctl restart` inside a `pwsh | tail` pipeline left postgres holding the pipe.
  - Stopping that task also removed the console the postmaster had inherited. Children then failed with `0xC0000142` and `error 487`, and the server could not shut down.
  - I terminated this instance's own processes after checking them by command line. PostgreSQL then recovered from WAL at start-up.
  - The script now starts `pg_ctl` through ShellExecute (`0d18dcf`); start and restart behind a pipe take about 1 s.
- **Windows limitation:** `client_connection_check_interval` is unsupported (R7). A killed index build can complete with nobody's decision; the ledger and `recover` handle it.

## 6. Remaining limitations

- **One operation family.** Single-table b-tree indexes on AND-ed equality/range predicates over plain columns. Not covered: expression, partial or covering indexes, multi-index plans, dropping existing indexes, configuration changes. Without hypopg the benefit is only known after building.
- **Least privilege has a gap.** The operator owns the tables through role membership. The event trigger blocks other DDL but not DML or `TRUNCATE`; the probe confirmed TRUNCATE is possible, inside a rolled-back transaction. The policy is what confines it.
- **Measurement design.**
  - The workload is declared and synthetic: `pg_stat_statements` is read, but production traffic is not replayed.
  - Before and after run sequentially, not interleaved; drift is bounded by the A/A check but not eliminated.
  - Thresholds are fixed policy values.
  - Client and server share one host.
- **Untested paths.**
  - Free space is only verifiable for a local `data_directory`; remote targets are blocked.
  - A long transaction that starts between validation and build is untested: it is expected to hit `lock_timeout` and take the recovery path.
- **Agent citations are checked syntactically.** The ids must exist, but a cited item is not proven to support the claim. Agent quality with an 8B model is uneven; the design tolerates that because agents never decide.
- **Concurrency and run modes.** One operation at a time, per server process. API-launched operations run in a server thread; if the server dies, `recover` is needed. No CI, no push.
- **Scope.** SQL Server is not implemented (the protocol only). The Tauri bundle still targets deb/appimage only.

## 7. Deliverables

1. **Repaired and verified Windows baseline:** §1, commits `4b83404`…`9eca865`, logs in `_qualification/db01/logs/` and `_qualification/db01/phase3/logs/final_baseline/`.
2. **PostgreSQL adapter:** `fieldhorizon/dbeng/adapters/postgres.py`.
3. **Autonomous indexing demonstration:** R2, with the full chain in its `report.md`.
4. **Before/after report:** §4, plus each operation's `report.md` and `decision.json`.
5. **Acceptance and rejection tests:** `tests/test_dbeng_*.py` (6 against real PostgreSQL).
6. **Exact commands:** `docs/dbeng/RUNBOOK.md`.
7. **This report.**
