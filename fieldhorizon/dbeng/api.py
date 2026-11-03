"""
FastAPI routes for DB engineering, mounted by fieldhorizon.server.create_app.

Same bearer token and loopback-only server as every other route. Operations
run in server-owned threads (service.DbService): the UI only reads state and
posts typed requests -- a launch with a client request id (idempotent), a
control (pause / resume / cancel), a recovery. No route accepts SQL, a file
path, or anything the executor would send as text; workloads are referenced
by the name of a file shipped in config/dbeng_workloads.

A graphical confirmation never replaces a backend check: every rule the UI
shows is enforced here or below (policy, executor, server-side guards).
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import AppConfig
from ..events import ACTOR_SERVER
from . import views
from .journal import Journal
from .service import WORKLOADS_DIR, DbService, ServiceError

# --- models (the frozen contract: contract/openapi.json -> contract/types.gen.ts) ------------------


class DbTargetModel(BaseModel):
    name: str
    engine: str
    host: str
    port: int
    dbname: str
    environment: str
    observer_user: str
    operator_user: str | None
    writable: bool
    reachable: bool | None = None
    server_version: int | None = None
    identity_matches: bool | None = None
    detail: str = ""


class DbTargetsResponse(BaseModel):
    targets: list[DbTargetModel]
    targets_file: str


class DbWorkloadModel(BaseModel):
    name: str
    file: str
    version: int = 1
    queries: list[dict[str, Any]]


class DbWorkloadsResponse(BaseModel):
    workloads: list[DbWorkloadModel]


class DbSnapshotModel(BaseModel):
    snapshot_id: str
    target: str
    captured_at: str
    server_version: str | None
    system_identifier: str | None
    sha256: str
    summary: dict[str, Any]


class DbSnapshotsResponse(BaseModel):
    snapshots: list[DbSnapshotModel]


class DbDiscoverRequest(BaseModel):
    target: str


class DbOperationModel(BaseModel):
    operation_id: str
    kind: str
    target: str
    status: str
    state: str
    message: str | None
    started_at: str
    finished_at: str | None


class DbOperationsResponse(BaseModel):
    operations: list[DbOperationModel]
    running: str | None


class DbOperationDetailResponse(BaseModel):
    operation: DbOperationModel
    request: dict[str, Any]
    decision: dict[str, Any] | None
    steps: list[dict[str, Any]]
    changes: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    verdicts: dict[str, Any]


class DbOptimizeRequest(BaseModel):
    target: str
    workload: str = "bench_orders.yaml"
    candidates: list[str] = []
    use_llm: bool = False
    llm_model: str | None = None
    # Idempotency: the same request id always designates the same operation (double click, refresh, retry).
    request_id: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")


class DbOptimizeResponse(BaseModel):
    operation_id: str
    status: str
    created: bool = True


class DbRecoverRequest(BaseModel):
    target: str


class DbRecoverResponse(BaseModel):
    target: str
    actions: list[dict[str, Any]]
    run_id: str | None = None
    status: str | None = None
    message: str | None = None
    mode: str | None = None
    blocked: list[dict[str, Any]] = []
    detection: dict[str, Any] | None = None


class DbOperationSummary(BaseModel):
    operation_id: str
    kind: str
    target: str
    status: str
    state: str
    cause: str | None
    message: str | None
    started_at: str
    finished_at: str | None
    live: bool
    workload: str | None
    candidate: str | None
    outcome: str | None
    weighted_ratio: float | None
    recovery_mode: str | None
    recovered_by: str | None
    lease_token: int | None
    llm: bool


class DbLiveState(BaseModel):
    checked_at: str
    reachable: bool | None = None
    detail: str | None = None
    server_version: str | None = None
    server_version_num: int | None = None
    instance_marker_live: str | None = None
    system_identifier_live: str | None = None
    identity_verified: bool | None = None
    in_recovery: bool | None = None
    detection: dict[str, Any] | None = None
    fieldhorizon_indexes: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    build_progress: list[dict[str, Any]] = []


class DbTargetCard(BaseModel):
    name: str
    engine: str
    host: str
    port: int
    dbname: str
    environment: str
    writable: bool
    data_classification: str
    identities: dict[str, str | None]
    pinned: dict[str, str | None]
    fingerprint: str
    running_operation: str | None
    reconciling: bool
    startup: dict[str, Any] | None
    live: DbLiveState | None = None


class DbPolicySummary(BaseModel):
    version: int
    sha256: str
    source: str
    recovery_automatic: bool
    max_authorization_age_seconds: float
    orphan_wait_seconds: float


class DbOverviewResponse(BaseModel):
    generated_at: str
    policy: DbPolicySummary
    targets: list[DbTargetCard]
    running: list[DbOperationSummary]
    recent: list[DbOperationSummary]
    recoveries: list[DbOperationSummary]
    counts: dict[str, int]
    startup: dict[str, Any]


class DbTargetDetailResponse(BaseModel):
    card: DbTargetCard
    authority: dict[str, Any] | None
    authority_detail: str | None = None
    snapshot: dict[str, Any] | None
    statements: dict[str, Any] | None


class DbOperationsPage(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[DbOperationSummary]
    running: dict[str, str]


class DbOperationView(BaseModel):
    operation: DbOperationSummary
    request: dict[str, Any]
    runtime: dict[str, Any] | None
    controls_available: list[str]
    timeline: list[dict[str, Any]]
    controls: list[dict[str, Any]]
    changes: list[dict[str, Any]]
    decision: dict[str, Any] | None
    candidates: list[dict[str, Any]]
    verdicts: dict[str, Any]
    authority: dict[str, Any] | None
    authorization: dict[str, Any] | None
    change: dict[str, Any] | None
    compensation: dict[str, Any] | None
    final_state: dict[str, Any] | None
    claims: dict[str, Any] | None
    recovery: dict[str, Any] | None
    execution_failure: dict[str, Any] | None
    lease: dict[str, Any] | None
    artifacts: list[dict[str, Any]]
    report_available: bool
    measurements_available: bool


class DbControlRequest(BaseModel):
    action: Literal["pause", "resume", "cancel"]


class DbControlResponse(BaseModel):
    operation_id: str
    seq: int
    action: str
    semantics: str


class DbMeasurementsResponse(BaseModel):
    operation_id: str
    queries: list[dict[str, Any]]
    weighted: dict[str, Any] | None
    side_effects: dict[str, Any] | None
    outcome: str | None
    units: dict[str, str]


class DbVerificationResponse(BaseModel):
    operation_id: str
    checked_at: str
    integrity: dict[str, Any]
    recomputation: dict[str, Any]
    attestation: dict[str, Any]


class DbPolicyResponse(BaseModel):
    version: int
    sha256: str
    source: str
    document: dict[str, Any]
    text: str | None


class DbSettingsResponse(BaseModel):
    targets_file: str
    workloads_dir: str
    journal_database: str
    evidence_root: str
    policy_source: str
    llm_model: str
    passfile_configured: dict[str, bool]
    controls: dict[str, str]


class DbDetectionResponse(BaseModel):
    target: str
    detection: dict[str, Any]


SERVICE_ERRORS = {
    "operation_running": ("An operation is already running on this target",
                          "Wait for it to finish, or pause or cancel it from its operation sheet."),
    "target_reconciling": ("Start-up reconciliation in progress", "The target is being checked after start-up; retry in a few seconds."),
    "request_id_reused": ("Request id already used", "A different launch needs a new request id."),
    "unknown_target": ("Unknown target", "Pick a target declared in the targets file (Settings)."),
    "targets_unavailable": ("No target file", "Run `field-horizon db bench-setup` or write the targets file named in Settings."),
    "unknown_workload": ("Unknown workload", "Pick one of the workloads listed on the Workloads screen."),
    "unknown_operation": ("Unknown operation", "Check the operation id."),
    "operation_finished": ("Operation already ended", "Nothing to control: the operation has a terminal status."),
    "not_controllable": ("Not controllable", "Only index optimizations can be paused, resumed or cancelled."),
    "already_paused": ("Already paused", "Resume or cancel it."),
    "not_paused": ("Not paused", "Only a paused operation can be resumed."),
    "already_cancelling": ("Already cancelling", "The cancel takes effect at the next safe point."),
}


def _operation_model(record) -> DbOperationModel:
    return DbOperationModel(
        operation_id=record.operation_id, kind=record.kind, target=record.target, status=record.status,
        state=record.state, message=record.message, started_at=record.started_at, finished_at=record.finished_at,
    )


def register_db_routes(app: FastAPI, cfg: AppConfig, require_token, open_adapter=None, open_executor=None,
                       service: DbService | None = None) -> DbService:
    svc = service or DbService(cfg, open_adapter=open_adapter, open_executor=open_executor)
    app.state.db_service = svc

    def call(fn, *args, **kwargs):
        from ..server import FieldHorizonAPIError

        try:
            return fn(*args, **kwargs)
        except ServiceError as exc:
            title, remediation = SERVICE_ERRORS.get(exc.code, ("Request refused", "See detail for what to do next."))
            raise FieldHorizonAPIError(exc.status_code, f"DB_{exc.code.upper()}", title, exc.detail, remediation) from exc

    # --- targets, workloads, snapshots (DB-01 routes, kept) -----------------------------------
    @app.get("/db/targets", response_model=DbTargetsResponse, dependencies=[Depends(require_token)])
    def get_db_targets(check: bool = False) -> DbTargetsResponse:
        known = call(svc.targets)
        out = []
        for t in known.values():
            model = DbTargetModel(
                name=t.name, engine=t.engine, host=t.host, port=t.port, dbname=t.dbname, environment=t.environment,
                observer_user=t.observer_user, operator_user=t.operator_user, writable=t.writable_environment,
            )
            if check:
                live = svc.live(t, max_age=0)
                model.reachable = live.get("reachable")
                model.server_version = live.get("server_version_num")
                model.identity_matches = live.get("identity_verified")
                model.detail = live.get("detail") or ""
            out.append(model)
        return DbTargetsResponse(targets=out, targets_file=str(svc.targets_path))

    @app.get("/db/targets/{name}", response_model=DbTargetDetailResponse, dependencies=[Depends(require_token)])
    def get_db_target(name: str) -> DbTargetDetailResponse:
        target = call(svc.target, name)
        card = views.target_card(svc, target)
        authority, authority_detail, statements = None, None, None
        adapter = None
        try:
            adapter = svc.open_adapter(target)
            authority = adapter.authority_facts()
            statements = adapter.statement_statistics()
        except Exception as exc:  # noqa: BLE001 - shown as unavailable with the reason
            from .targets import redact

            authority_detail = redact(f"{type(exc).__name__}: {exc}")[:300]
        finally:
            if adapter is not None:
                adapter.close()
        snaps = Journal(cfg).snapshots(name, limit=1)
        return DbTargetDetailResponse(card=DbTargetCard(**card), authority=authority, authority_detail=authority_detail,
                                      snapshot=snaps[0] if snaps else None, statements=statements)

    @app.get("/db/workloads", response_model=DbWorkloadsResponse, dependencies=[Depends(require_token)])
    def get_db_workloads() -> DbWorkloadsResponse:
        from .workload import load_workload

        items = []
        for path in sorted(WORKLOADS_DIR.glob("*.yaml")):
            w = load_workload(path)
            items.append(DbWorkloadModel(name=w.name, file=path.name, version=w.version, queries=[
                {"id": q.query_id, "kind": q.kind, "weight": q.weight, "sql": q.sql, "control": q.control,
                 "repeat": q.repeat, "param_sets": len(q.params)} for q in w.queries
            ]))
        return DbWorkloadsResponse(workloads=items)

    @app.get("/db/snapshots", response_model=DbSnapshotsResponse, dependencies=[Depends(require_token)])
    def get_db_snapshots(target: str | None = None, limit: int = 20) -> DbSnapshotsResponse:
        rows = Journal(cfg).snapshots(target, limit=limit)
        return DbSnapshotsResponse(snapshots=[DbSnapshotModel(**{k: r[k] for k in DbSnapshotModel.model_fields}) for r in rows])

    @app.post("/db/discover", response_model=DbSnapshotModel, dependencies=[Depends(require_token)])
    def post_db_discover(body: DbDiscoverRequest) -> DbSnapshotModel:
        from .advisor import structural_findings

        target = call(svc.target, body.target)
        adapter = svc.open_adapter(target)
        try:
            identity = adapter.identity()
            snapshot = adapter.discover()
        finally:
            adapter.close()
        snapshot.update(server=identity, target=target.describe())
        snapshot["findings"] = structural_findings(snapshot)
        recorded = Journal(cfg).record_snapshot(target.name, snapshot)
        row = Journal(cfg).snapshot(recorded["snapshot_id"])
        assert row is not None
        return DbSnapshotModel(**{k: row[k] for k in DbSnapshotModel.model_fields})

    # --- overview, policy, settings ----------------------------------------------------------
    @app.get("/db/overview", response_model=DbOverviewResponse, dependencies=[Depends(require_token)])
    def get_db_overview() -> DbOverviewResponse:
        return DbOverviewResponse(**call(views.overview, svc))

    @app.get("/db/policy", response_model=DbPolicyResponse, dependencies=[Depends(require_token)])
    def get_db_policy() -> DbPolicyResponse:
        return DbPolicyResponse(**views.policy_view(svc.policy()))

    @app.get("/db/settings", response_model=DbSettingsResponse, dependencies=[Depends(require_token)])
    def get_db_settings() -> DbSettingsResponse:
        from .service import CONTROL_SEMANTICS

        try:
            known = svc.targets()
        except ServiceError:
            known = {}
        return DbSettingsResponse(
            targets_file=str(svc.targets_path), workloads_dir=str(WORKLOADS_DIR), journal_database=str(cfg.database),
            evidence_root=str(cfg.outputs / "dbeng"), policy_source=str(svc.policy().source), llm_model=cfg.default_model,
            passfile_configured={name: bool(t.passfile) for name, t in known.items()}, controls=CONTROL_SEMANTICS,
        )

    # --- operations -------------------------------------------------------------------------
    @app.get("/db/operations", response_model=DbOperationsResponse, dependencies=[Depends(require_token)])
    def get_db_operations(limit: int = 30) -> DbOperationsResponse:
        running = svc.running()
        return DbOperationsResponse(operations=[_operation_model(r) for r in Journal(cfg).recent(limit)],
                                    running=next(iter(running.values()), None))

    @app.get("/db/operations-page", response_model=DbOperationsPage, dependencies=[Depends(require_token)])
    def get_db_operations_page(target: str | None = None, status: str | None = None, kind: str | None = None, q: str | None = None,
                               sort: str = "started_at", order: Literal["asc", "desc"] = "desc", offset: int = 0,
                               limit: int = 25) -> DbOperationsPage:
        journal = Journal(cfg)
        total, rows = journal.query(target=target, status=status, kind=kind, text=q, sort=sort, descending=order == "desc",
                                    offset=offset, limit=limit)
        return DbOperationsPage(total=total, offset=offset, limit=limit, running=svc.running(),
                                items=[DbOperationSummary(**views.operation_summary(journal, r)) for r in rows])

    @app.get("/db/operations/{operation_id}", response_model=DbOperationDetailResponse, dependencies=[Depends(require_token)])
    def get_db_operation(operation_id: str) -> DbOperationDetailResponse:
        view = views.operation_view(cfg, operation_id)
        if view is None:
            raise HTTPException(status_code=404, detail=f"No operation {operation_id}")
        record = Journal(cfg).get(operation_id)
        assert record is not None
        return DbOperationDetailResponse(
            operation=_operation_model(record), request=view["request"], decision=view["decision"], steps=view["timeline"],
            changes=view["changes"], artifacts=view["artifacts"], candidates=view["candidates"], verdicts=view["verdicts"],
        )

    @app.get("/db/operations/{operation_id}/view", response_model=DbOperationView, dependencies=[Depends(require_token)])
    def get_db_operation_view(operation_id: str) -> DbOperationView:
        view = views.operation_view(cfg, operation_id)
        if view is None:
            raise HTTPException(status_code=404, detail=f"No operation {operation_id}")
        return DbOperationView(**view)

    @app.get("/db/operations/{operation_id}/measurements", response_model=DbMeasurementsResponse, dependencies=[Depends(require_token)])
    def get_db_operation_measurements(operation_id: str) -> DbMeasurementsResponse:
        view = views.measurements_view(cfg, operation_id)
        if view is None:
            raise HTTPException(status_code=404, detail=f"No operation {operation_id}")
        return DbMeasurementsResponse(**view)

    @app.get("/db/operations/{operation_id}/verify", response_model=DbVerificationResponse, dependencies=[Depends(require_token)])
    def get_db_operation_verify(operation_id: str) -> DbVerificationResponse:
        from .export import verification

        if Journal(cfg).get(operation_id) is None:
            raise HTTPException(status_code=404, detail=f"No operation {operation_id}")
        return DbVerificationResponse(**verification(cfg, operation_id))

    @app.get("/db/operations/{operation_id}/artifacts/{name}", dependencies=[Depends(require_token)])
    def get_db_operation_artifact(operation_id: str, name: str, redact: bool = True) -> PlainTextResponse:
        from .export import redact_document

        artifact = next((a for a in Journal(cfg).artifacts(operation_id) if a["name"] == name), None)
        if artifact is None or not Path(artifact["path"]).exists():
            raise HTTPException(status_code=404, detail=f"No artifact {name!r} for {operation_id}")
        text = Path(artifact["path"]).read_text(encoding="utf-8")
        if redact and name.endswith(".json"):
            text = json.dumps(redact_document(json.loads(text)), indent=2, sort_keys=True, ensure_ascii=False)
        media = "application/json" if name.endswith(".json") else "text/markdown" if name.endswith(".md") else "text/plain"
        return PlainTextResponse(text, media_type=media)

    @app.get("/db/operations/{operation_id}/report", response_class=PlainTextResponse, dependencies=[Depends(require_token)])
    def get_db_operation_report(operation_id: str) -> PlainTextResponse:
        report = next((a for a in Journal(cfg).artifacts(operation_id) if a["name"] == "report.md"), None)
        if report is None:
            raise HTTPException(status_code=404, detail=f"No report for {operation_id} (still running?)")
        return PlainTextResponse(Path(report["path"]).read_text(encoding="utf-8"), media_type="text/markdown")

    @app.get("/db/operations/{operation_id}/evidence", dependencies=[Depends(require_token)])
    def get_db_operation_evidence(operation_id: str, redact: bool = True) -> StreamingResponse:
        from .export import build_bundle

        record = Journal(cfg).get(operation_id)
        if record is None or not Journal(cfg).artifacts(operation_id):
            raise HTTPException(status_code=404, detail=f"No evidence for {operation_id}")
        classification = "unspecified"  # an unknown target is exported under the strictest rules
        with contextlib.suppress(ServiceError):
            classification = svc.target(record.target).data_classification
        if not redact and classification != "synthetic":
            raise HTTPException(status_code=403, detail="Unredacted export is only available for a synthetic lab target.")
        data = build_bundle(cfg, operation_id, redact=redact, data_classification=classification)
        return StreamingResponse(io.BytesIO(data), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{operation_id}-evidence.zip"'})

    @app.post("/db/operations", response_model=DbOptimizeResponse, status_code=202, dependencies=[Depends(require_token)])
    def post_db_operation(body: DbOptimizeRequest) -> DbOptimizeResponse:
        launch = call(svc.start_optimization, target=body.target, workload=body.workload, candidates=body.candidates,
                      use_llm=body.use_llm, llm_model=body.llm_model, request_id=body.request_id, actor=ACTOR_SERVER)
        return DbOptimizeResponse(operation_id=launch.operation_id, status=launch.status, created=launch.created)

    @app.post("/db/operations/{operation_id}/control", response_model=DbControlResponse, dependencies=[Depends(require_token)])
    def post_db_operation_control(operation_id: str, body: DbControlRequest) -> DbControlResponse:
        return DbControlResponse(**call(svc.control, operation_id, body.action))

    # --- incidents and recovery ---------------------------------------------------------------
    @app.get("/db/targets/{name}/incidents", response_model=DbDetectionResponse, dependencies=[Depends(require_token)])
    def get_db_incidents(name: str) -> DbDetectionResponse:
        return DbDetectionResponse(target=name, detection=call(svc.detect, name))

    @app.post("/db/recover", response_model=DbRecoverResponse, dependencies=[Depends(require_token)])
    def post_db_recover(body: DbRecoverRequest) -> DbRecoverResponse:
        report = call(svc.recover, body.target)
        return DbRecoverResponse(target=body.target, actions=report["actions"], run_id=report["run_id"], status=report["status"],
                                 message=report["message"], mode=report["mode"], blocked=report["blocked"],
                                 detection=report["detection"])

    return svc
