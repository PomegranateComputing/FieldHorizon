# Field Horizon DB engineering — runbook (Windows 11, this PC)

Exact commands to rebuild the isolated PostgreSQL test instance, the benchmark, and to rerun the
autonomous index-optimization demonstration and its acceptance/rejection scenarios. PowerShell 7,
from the repository root `C:\PROJECTS\FieldHorizon`.

Nothing here touches a production or pre-existing database: the test instance is a separate
cluster on `127.0.0.1:55432`, started as the current user, with no Windows service.

```powershell
$REPO = "C:\PROJECTS\FieldHorizon"
$PY   = "$REPO\_qualification\windows\env\Scripts\python.exe"   # Python 3.12.10 venv: pip install -e ".[dev,vec,pg]"
$PG   = "$REPO\_qualification\db01\pg"                           # PostgreSQL 18.6 binaries + cluster (gitignored)
$INST = "$REPO\scripts\dbeng\pg-test-instance.ps1"
$P3   = "$REPO\_qualification\db01\phase3"                       # Field Horizon config + journal for the demo
$CFG  = "$P3\config.yaml"
$PASS = "$env:LOCALAPPDATA\FieldHorizon\dbeng\pgpass.conf"      # libpq password file, owner-only ACL, never in Git
Set-Location $REPO
```

## 1. PostgreSQL binaries (once)

No PostgreSQL was installed on this PC. The official EDB "binaries" archive was used, without its
installer: <https://www.enterprisedb.com/download-postgresql-binaries> → Windows x86-64, 18.6
(`postgresql-18.6-5-windows-x64-binaries.zip`, 384 620 317 bytes, SHA-256
`e2246ba91d22345bc3d017586c09ede52d9df180b1eeb480f050445f1cad84e2`). The executables in it are **not
Authenticode-signed**; integrity rests on HTTPS from the official host and the recorded hash.

Only the server/client parts are extracted (145 MB instead of 1 GB; pgAdmin and docs are skipped):

```powershell
New-Item -ItemType Directory -Force "$PG\download" | Out-Null
curl.exe -sSL -o "$PG\download\postgresql-18.6-windows-x64-binaries.zip" "https://sbp.enterprisedb.com/getfile.jsp?fileid=1260609"
(Get-FileHash "$PG\download\postgresql-18.6-windows-x64-binaries.zip").Hash   # compare with the hash above
Set-Location $PG
unzip -q download\postgresql-18.6-windows-x64-binaries.zip "pgsql/bin/**" "pgsql/lib/**" "pgsql/share/**" "pgsql/doc/src/sgml/html/sql-createindex.html"
Set-Location $REPO
```

(`unzip` is Git for Windows'; `Expand-Archive` on the whole archive also works but extracts 1 GB.)

## 2. Isolated test instance

```powershell
& $INST -Action init    -PgBin "$PG\pgsql\bin" -DataDir "$PG\cluster" -Port 55432   # generates the superuser secret into $PASS
& $INST -Action start   -PgBin "$PG\pgsql\bin" -DataDir "$PG\cluster" -Port 55432
& $INST -Action status  -PgBin "$PG\pgsql\bin" -DataDir "$PG\cluster" -Port 55432
& $INST -Action restart -PgBin "$PG\pgsql\bin" -DataDir "$PG\cluster" -Port 55432
& $INST -Action stop    -PgBin "$PG\pgsql\bin" -DataDir "$PG\cluster" -Port 55432
& $INST -Action destroy -PgBin "$PG\pgsql\bin" -DataDir "$PG\cluster" -Port 55432   # stopped instance only
```

Settings are recorded in every discovery snapshot: loopback only, SCRAM-SHA-256 always, `jit = off`,
`max_parallel_workers_per_gather = 0` (measurement stability), `pg_stat_statements` preloaded.

## 3. Benchmark database and target file

```powershell
& $PY -m fieldhorizon.cli --config $CFG db bench-setup --reset --port 55432 --passfile $PASS --data-directory "$PG\cluster"
& $PY -m fieldhorizon.cli --config $CFG db targets
& $PY -m fieldhorizon.cli --config $CFG db discover
```

`bench-setup` creates `fh_observer` (read-only) and `fh_operator` (index DDL only, enforced by an
event trigger), rotates their passwords into `$PASS`, builds `fh_bench` (2 000 000 orders whose
`customer_id` foreign key has no index), pins the instance marker and system identifier, and writes
`$P3\dbeng_targets.yaml` (no secret in it).

## 4. The demonstration and its scenarios

```powershell
$DOCS = "$PG\pgsql\doc\src\sgml\html"
$HEAVY = "config\dbeng_workloads\bench_orders_write_heavy.yaml"

# R2 — the autonomous loop, with the LLM agents (advisory) and documentary evidence verified: RETAINED
& $PY -m fieldhorizon.cli --config $CFG db optimize-indexes --llm --pg-docs $DOCS
# R3 — the same operation again: NO_CANDIDATE (idempotent)
& $PY -m fieldhorizon.cli --config $CFG db optimize-indexes
# R4 — poor and prohibited candidates: REJECTED, nothing written
& $PY -m fieldhorizon.cli --config $CFG db optimize-indexes --candidate "bench.orders(status)" --candidate "bench.audit_log(event_type)" --candidate "pg_catalog.pg_class(relname)"
# R5 — a write-dominated workload: the index is built, measured, and REVERTED
& $PY -m fieldhorizon.cli --config $CFG db optimize-indexes --workload $HEAVY
# R6 — crash right after the change (test-only fault injection, exit code 86), restart, recover
& $PY -m fieldhorizon.cli --config $CFG db optimize-indexes --workload $HEAVY --fault-after apply
& $INST -Action restart -PgBin "$PG\pgsql\bin" -DataDir "$PG\cluster" -Port 55432
& $PY -m fieldhorizon.cli --config $CFG db optimize-indexes          # BLOCKED until recovery
& $PY -m fieldhorizon.cli --config $CFG db recover                   # RECOVERED_REVERTED
& $PY -m fieldhorizon.cli --config $CFG db recover                   # nothing to do (idempotent)
& $PY -m fieldhorizon.cli --config $CFG db bench-verify              # amcheck on every b-tree, no invalid index
# R7 — a real external kill during CREATE INDEX CONCURRENTLY
& $PY _qualification\db01\tools\kill_during_apply.py $PY $CFG "$P3\data\fh_phase3.sqlite3" $HEAVY "$P3\logs\killed.log" 0.4
& $PY -m fieldhorizon.cli --config $CFG db recover
# R8 — authority: declare a target "production" (or pin a wrong marker) in dbeng_targets.yaml, then
& $PY -m fieldhorizon.cli --config $CFG db optimize-indexes --target bench_local_declared_production   # BLOCKED
```

Inspection and evidence:

```powershell
& $PY -m fieldhorizon.cli --config $CFG db operations
& $PY -m fieldhorizon.cli --config $CFG db operation <OPERATION_ID>
& $PY -m fieldhorizon.cli --config $CFG db evidence <OPERATION_ID> --output "$P3\evidence_copy"   # verifies every sha256
& $PY -m fieldhorizon.cli --config $CFG db snapshots
```

Every operation writes `$P3\outputs\dbeng\operations\<id>\` (report.md, operation.json,
measurements, verdicts, decision…) and registers each file's SHA-256 in the journal.

## 5. Operator interface

```powershell
& $PY -m fieldhorizon.cli --config $CFG serve --port 8777 --token-file "$P3\.fh_token"
```

Open <http://127.0.0.1:8777/ui/>, paste the content of `$P3\.fh_token`, go to **DATABASE**. The
native app (`apps\desktop\src-tauri\target\release\desktop.exe`, built by `npx tauri build`) reaches
the same backend on `127.0.0.1:8777`.

## 6. Tests

```powershell
& $PY -m pytest -q                                   # unit + API; PostgreSQL tests skip without the variables below
$env:FIELDHORIZON_DBENG_TEST_PASSFILE = $PASS
$env:FIELDHORIZON_DBENG_TEST_DATA_DIR = "$PG\cluster"
& $PY -m pytest -q tests\test_dbeng_postgres.py      # builds its own fh_bench_pytest database (300k orders)
& $PY -m ruff check fieldhorizon\ tests\ scripts\
& $PY -m mypy fieldhorizon\
Set-Location apps\desktop; npx tsc --noEmit; npx vitest run; npm run build; Set-Location $REPO
```

## Known Windows specifics

- `client_connection_check_interval` "must be set to 0 on this platform": a server cannot notice a
  dead client mid-statement. A killed `CREATE INDEX CONCURRENTLY` may still complete server-side,
  leaving a valid index without its Field Horizon marker; the change ledger lets `db recover` find
  and remove it (observed in R7).
- `pg_ctl` must not be started from a pipeline or a console that will disappear: the server
  inherits every inheritable handle. The instance script starts it through ShellExecute.
- `uv lock` downloads the Python named in `.python-version` (3.13) unless told otherwise:
  `uv lock --python 3.12` or `$env:UV_PYTHON_DOWNLOADS = "never"`.
