"""`field-horizon db ...` -- the operator interface of fieldhorizon.dbeng."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ..config import AppConfig
from ..config import load_config as _load_config
from ..db import init_db
from .journal import Journal
from .targets import TargetConfig, default_targets_path, load_targets

console = Console()

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_WORKLOAD = PACKAGE_ROOT / "config" / "dbeng_workloads" / "bench_orders.yaml"
FAULT_EXIT_CODE = 86


def load_config(path) -> AppConfig:
    """Every db command journals into the Field Horizon database: make sure it exists (events, provenance, dbeng tables)."""
    cfg = _load_config(path)
    init_db(cfg.database)
    return cfg


def _targets_path(args, cfg) -> Path:
    return Path(args.targets) if args.targets else default_targets_path(cfg.root)


def _target(args, cfg) -> TargetConfig:
    targets = load_targets(_targets_path(args, cfg))
    name = args.target or next(iter(targets))
    if name not in targets:
        raise SystemExit(f"Unknown target {name!r}; known: {sorted(targets)}")
    return targets[name]


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def cmd_targets(args) -> None:
    from .adapters import open_adapter

    cfg = load_config(args.config)
    table = Table(title="DB engineering targets")
    for col in ("target", "engine", "endpoint", "environment", "observer", "operator", "reachable", "marker", "system id"):
        table.add_column(col)
    for name, target in load_targets(_targets_path(args, cfg)).items():
        adapter = open_adapter(target)
        try:
            ident = adapter.identity()
            reachable = f"yes ({ident['version_num']})"
            marker = "match" if ident.get("instance_marker") == target.instance_marker else f"MISMATCH/{ident.get('instance_marker_status')}"
            sysid = "match" if ident.get("system_identifier") == target.system_identifier else f"MISMATCH/{ident.get('system_identifier_status')}"
        except Exception as exc:
            reachable, marker, sysid = f"no: {str(exc)[:80]}", "-", "-"
        finally:
            adapter.close()
        table.add_row(name, target.engine, f"{target.host}:{target.port}/{target.dbname}", target.environment,
                      target.observer_user, target.operator_user or "-", reachable, marker, sysid)
    console.print(table)


def cmd_discover(args) -> None:
    from .adapters import open_adapter
    from .advisor import structural_findings

    cfg = load_config(args.config)
    target = _target(args, cfg)
    adapter = open_adapter(target)
    try:
        identity = adapter.identity()
        snapshot = adapter.discover()
    finally:
        adapter.close()
    snapshot["server"] = identity
    snapshot["target"] = target.describe()
    snapshot["findings"] = structural_findings(snapshot)
    recorded = Journal(cfg).record_snapshot(target.name, snapshot)
    if args.json:
        _print_json(recorded)
        return
    console.print(f"Snapshot [bold]{recorded['snapshot_id']}[/bold] (sha256 {recorded['sha256'][:16]}...) -> {recorded['path']}")
    console.print(f"{recorded['summary']['tables']} tables, {recorded['summary']['indexes']} indexes, "
                  f"{recorded['summary']['constraints']} constraints")
    for feature, status in recorded["summary"]["availability"].items():
        console.print(f"  availability {feature}: {status}")
    for finding in snapshot["findings"]:
        console.print(f"  [yellow]finding[/yellow] {finding['kind']}: {finding['object']} {finding.get('detail', '')}")


def cmd_snapshots(args) -> None:
    cfg = load_config(args.config)
    _print_json(Journal(cfg).snapshots(args.target, limit=args.limit))


def _fault_exit(step: str) -> None:
    console.print(f"[red]--fault-after {step}: simulating a crash (hard exit {FAULT_EXIT_CODE}, no cleanup)[/red]")
    os._exit(FAULT_EXIT_CODE)


def cmd_optimize_indexes(args) -> None:
    from .adapters import open_adapter
    from .operation import IndexOptimization, OptimizeRequest
    from .policy import load_policy
    from .workload import load_workload

    cfg = load_config(args.config)
    target = _target(args, cfg)
    policy = load_policy(Path(args.policy) if args.policy else None)
    workload = load_workload(Path(args.workload))
    request = OptimizeRequest(
        target=target.name, workload_path=str(Path(args.workload).resolve()), candidates=args.candidate or [],
        use_llm=args.llm, llm_model=args.llm_model, docs_dir=args.pg_docs, fault_after=args.fault_after,
    )
    adapter = open_adapter(target)
    try:
        outcome = IndexOptimization(cfg, target, adapter, policy, workload, request, fault_hook=_fault_exit,
                                    executor_factory=_executor_factory(args, cfg, target, policy)).run()
    finally:
        adapter.close()
    if args.json:
        _print_json(asdict(outcome))
    else:
        console.print(f"Operation [bold]{outcome.operation_id}[/bold]: [bold]{outcome.status}[/bold] (cause {outcome.cause})")
        console.print(outcome.message)
        console.print(f"Evidence: {outcome.evidence_dir}")
    raise SystemExit(EXIT_CODES.get(outcome.status, 0))


EXIT_CODES = {"FAILED_NEEDS_RECOVERY": 2, "BLOCKED": 3, "FAILED_RECOVERED": 4}


def _executor_factory(args, cfg, target, policy):
    from .executor import PostgresExecutor

    def make(operation_id: str):
        return PostgresExecutor(target, policy, operation_id=operation_id, journal_ref=str(cfg.database),
                                targets_path=_targets_path(args, cfg))
    return make


def cmd_operations(args) -> None:
    cfg = load_config(args.config)
    records = Journal(cfg).recent(limit=args.limit)
    if args.json:
        _print_json([r.__dict__ for r in records])
        return
    table = Table(title="DB engineering operations")
    for col in ("operation", "target", "status", "state", "started", "message"):
        table.add_column(col)
    for r in records:
        table.add_row(r.operation_id, r.target, r.status, r.state, r.started_at[:19], (r.message or "")[:90])
    console.print(table)


def cmd_operation(args) -> None:
    from .operation import operation_detail

    cfg = load_config(args.config)
    detail = operation_detail(cfg, args.operation_id)
    if detail is None:
        raise SystemExit(f"No operation {args.operation_id}")
    _print_json(detail)


def cmd_recover(args) -> None:
    from .adapters import open_adapter
    from .policy import load_policy
    from .recovery import MODE_MANUAL, recover

    cfg = load_config(args.config)
    target = _target(args, cfg)
    policy = load_policy(Path(args.policy) if args.policy else None)
    adapter = open_adapter(target)
    try:
        report = recover(cfg, target, adapter, policy, _executor_factory(args, cfg, target, policy), mode=MODE_MANUAL)
    finally:
        adapter.close()
    _print_json({k: report[k] for k in ("run_id", "mode", "status", "message", "actions", "blocked", "orphan_wait")})
    raise SystemExit(0 if report["status"] in ("RECOVERY_DONE", "RECOVERY_NOTHING_TO_DO", "RECOVERY_DEFERRED") else 2)


def cmd_status(args) -> None:
    """Detection only: what is unfinished on the target and in this journal. Changes nothing."""
    from .adapters import open_adapter
    from .policy import load_policy
    from .recovery import safe_detect

    cfg = load_config(args.config)
    target = _target(args, cfg)
    adapter = open_adapter(target)
    try:
        detection = safe_detect(adapter, Journal(cfg), target, load_policy(Path(args.policy) if args.policy else None))
    finally:
        adapter.close()
    _print_json(detection)
    raise SystemExit(0 if detection.get("clean") else 1)


def cmd_control(args) -> None:
    from .service import DbService, ServiceError

    cfg = load_config(args.config)
    try:
        result = DbService(cfg, targets_path=_targets_path(args, cfg)).control(args.operation_id, args.action, actor="cli")
    except ServiceError as exc:
        raise SystemExit(f"{exc.code}: {exc.detail}") from None
    _print_json(result)


def cmd_verify(args) -> None:
    from .export import verification

    cfg = load_config(args.config)
    if Journal(cfg).get(args.operation_id) is None:
        raise SystemExit(f"No operation {args.operation_id}")
    result = verification(cfg, args.operation_id)
    _print_json(result)
    ok = result["integrity"]["all_intact"] and (not result["recomputation"].get("available") or result["recomputation"]["matches"])
    raise SystemExit(0 if ok else 1)


def cmd_evidence(args) -> None:
    import hashlib

    cfg = load_config(args.config)
    artifacts = Journal(cfg).artifacts(args.operation_id)
    if not artifacts:
        raise SystemExit(f"No evidence recorded for {args.operation_id}")
    out = Path(args.output) / args.operation_id
    out.mkdir(parents=True, exist_ok=True)
    report = []
    for a in artifacts:
        data = Path(a["path"]).read_bytes()
        ok = hashlib.sha256(data).hexdigest() == a["sha256"]
        shutil.copyfile(a["path"], out / a["name"])
        report.append({"name": a["name"], "sha256_verified": ok, "bytes": len(data)})
    bundle = None
    if args.zip:
        from .export import build_bundle

        record = Journal(cfg).get(args.operation_id)
        classification = "unspecified"  # an unknown target is exported under the strictest rules
        with contextlib.suppress(Exception):
            classification = load_targets(_targets_path(args, cfg))[record.target].data_classification if record else classification
        bundle = out.parent / f"{args.operation_id}-evidence-redacted.zip"
        bundle.write_bytes(build_bundle(cfg, args.operation_id, redact=True, data_classification=classification))
    _print_json({"operation_id": args.operation_id, "copied_to": str(out), "files": report,
                 "all_verified": all(r["sha256_verified"] for r in report), "redacted_bundle": str(bundle) if bundle else None})


def cmd_bench_setup(args) -> None:
    from .bench import BenchConfig, bench_setup

    cfg = load_config(args.config)
    passfile = Path(args.passfile or os.environ.get("PGPASSFILE", ""))
    if not passfile.name or not passfile.exists():
        raise SystemExit("A libpq password file with the admin entry is required (--passfile or PGPASSFILE).")
    bench = BenchConfig(
        host=args.host, port=args.port, admin_user=args.admin_user, passfile=passfile, data_directory=args.data_directory,
        targets_path=_targets_path(args, cfg), target_name=args.target or "bench_local",
        orders=args.orders, customers=args.customers, reset=args.reset,
    )
    target = bench_setup(bench, log=lambda m: console.print(f"[green]bench[/green] {m}"))
    console.print(f"Target {target.name}: marker {target.instance_marker}, system identifier {target.system_identifier}")


def cmd_bench_verify(args) -> None:
    from .adapters import open_adapter
    from .bench import bench_verify

    cfg = load_config(args.config)
    adapter = open_adapter(_target(args, cfg))
    try:
        result = bench_verify(adapter)
    finally:
        adapter.close()
    _print_json(result)
    raise SystemExit(0 if result["consistent"] else 1)


def register(sub) -> None:
    db = sub.add_parser("db", help="DB engineering: discovery, autonomous index optimization, recovery (fieldhorizon.dbeng).")
    dsub = db.add_subparsers(required=True)

    def common(p, target=True):
        p.add_argument("--targets", default=None, help="Target file (default: <config dir>/dbeng_targets.yaml or $FIELDHORIZON_DBENG_TARGETS).")
        if target:
            p.add_argument("--target", default=None, help="Target name (default: the first in the file).")
        return p

    p = common(dsub.add_parser("targets", help="List targets and check reachability and identity."), target=False)
    p.set_defaults(func=cmd_targets)

    p = common(dsub.add_parser("discover", help="Read-only discovery: versioned snapshot + structural findings."))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = dsub.add_parser("snapshots", help="List recorded snapshots.")
    p.add_argument("--target", default=None)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_snapshots)

    p = common(dsub.add_parser("optimize-indexes", help="Run one autonomous index optimization within policy."))
    p.add_argument("--workload", default=str(DEFAULT_WORKLOAD))
    p.add_argument("--policy", default=None, help="Policy file (default: config/dbeng_policy.yaml).")
    p.add_argument("--candidate", action="append", help="Extra candidate schema.table(col[, col]); same gate as any other.")
    p.add_argument("--llm", action="store_true", help="Run the LLM agent roles (advisory; never decisive).")
    p.add_argument("--llm-model", default=None)
    p.add_argument("--pg-docs", default=None, help="PostgreSQL HTML docs dir, to verify documentary evidence.")
    p.add_argument("--fault-after", default=None, help="TEST ONLY: hard-exit right after this step completes, to exercise recovery.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_optimize_indexes)

    p = dsub.add_parser("operations", help="List operations.")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_operations)

    p = dsub.add_parser("operation", help="Full journal of one operation.")
    p.add_argument("operation_id")
    p.set_defaults(func=cmd_operation)

    p = common(dsub.add_parser("recover", help="Manual recovery: bring unfinished changes back, proven ownership only; idempotent."))
    p.add_argument("--policy", default=None)
    p.set_defaults(func=cmd_recover)

    p = common(dsub.add_parser("status", help="Detection only: unfinished changes, orphaned activity, lease holder. Changes nothing."))
    p.add_argument("--policy", default=None)
    p.set_defaults(func=cmd_status)

    p = common(dsub.add_parser("control", help="Pause, resume or cancel a running operation (applied at its next safe point)."),
               target=False)
    p.add_argument("operation_id")
    p.add_argument("action", choices=["pause", "resume", "cancel"])
    p.set_defaults(func=cmd_control)

    p = dsub.add_parser("verify", help="Integrity of an operation's evidence and recomputation of its decision.")
    p.add_argument("operation_id")
    p.set_defaults(func=cmd_verify)

    p = common(dsub.add_parser("evidence", help="Copy an operation's evidence files and verify their sha256."), target=False)
    p.add_argument("operation_id")
    p.add_argument("--output", required=True)
    p.add_argument("--zip", action="store_true", help="Also write a redacted export bundle (literals masked, paths hidden).")
    p.set_defaults(func=cmd_evidence)

    p = common(dsub.add_parser("bench-setup", help="Build the reproducible benchmark database on an isolated test instance."))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=55432)
    p.add_argument("--admin-user", default="fh_admin")
    p.add_argument("--passfile", default=None)
    p.add_argument("--data-directory", default=None, help="Cluster data directory (enables the free-space check).")
    p.add_argument("--orders", type=int, default=2_000_000)
    p.add_argument("--customers", type=int, default=200_000)
    p.add_argument("--reset", action="store_true", help="Drop and rebuild the bench database.")
    p.set_defaults(func=cmd_bench_setup)

    p = common(dsub.add_parser("bench-verify", help="Consistency check: invalid indexes and amcheck on every bench b-tree."))
    p.set_defaults(func=cmd_bench_verify)
