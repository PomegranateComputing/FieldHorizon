"""
FastAPI routes for DB engineering, mounted by fieldhorizon.server.create_app.

Same bearer token and loopback-only server as every other route. An
operation started here runs in a background thread (one at a time) and
reports progress through its journal and the existing domain-event stream.
The API accepts only workload files shipped in config/dbeng_workloads (never
an arbitrary path) and never exposes the test-only fault injection.
"""
from __future__ import annotations

import io
import json
import threading
import zipfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from ..config import AppConfig
from ..events import ACTOR_SERVER
from .journal import Journal
from .targets import TargetConfigError, default_targets_path, load_targets

WORKLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "dbeng_workloads"


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


class DbOptimizeResponse(BaseModel):
    operation_id: str
    status: str


class DbRecoverRequest(BaseModel):
    target: str


class DbRecoverResponse(BaseModel):
    target: str
    actions: list[dict[str, Any]]


def _operation_model(record) -> DbOperationModel:
    return DbOperationModel(
        operation_id=record.operation_id, kind=record.kind, target=record.target, status=record.status,
        state=record.state, message=record.message, started_at=record.started_at, finished_at=record.finished_at,
    )


def register_db_routes(app: FastAPI, cfg: AppConfig, require_token, open_adapter=None) -> None:
    lock = threading.Lock()
    running: dict[str, str | None] = {"operation_id": None}

    def adapter_for(target):
        if open_adapter is not None:
            return open_adapter(target)
        from .adapters import open_adapter as default_open

        return default_open(target)

    def targets():
        path = default_targets_path(cfg.root)
        try:
            return path, load_targets(path)
        except TargetConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def target_or_404(name: str):
        _, known = targets()
        if name not in known:
            raise HTTPException(status_code=404, detail=f"Unknown target {name!r}")
        return known[name]

    @app.get("/db/targets", response_model=DbTargetsResponse, dependencies=[Depends(require_token)])
    def get_db_targets(check: bool = False) -> DbTargetsResponse:
        path, known = targets()
        out = []
        for t in known.values():
            model = DbTargetModel(
                name=t.name, engine=t.engine, host=t.host, port=t.port, dbname=t.dbname, environment=t.environment,
                observer_user=t.observer_user, operator_user=t.operator_user, writable=t.writable_environment,
            )
            if check:
                adapter = None
                try:
                    adapter = adapter_for(t)
                    ident = adapter.identity()
                    model.reachable = True
                    model.server_version = ident.get("version_num")
                    model.identity_matches = (ident.get("instance_marker") == t.instance_marker
                                              and ident.get("system_identifier") == t.system_identifier)
                except Exception as exc:
                    model.reachable, model.detail = False, str(exc)[:300]
                finally:
                    if adapter is not None:
                        adapter.close()
            out.append(model)
        return DbTargetsResponse(targets=out, targets_file=str(path))

    @app.get("/db/workloads", response_model=DbWorkloadsResponse, dependencies=[Depends(require_token)])
    def get_db_workloads() -> DbWorkloadsResponse:
        from .workload import load_workload

        items = []
        for path in sorted(WORKLOADS_DIR.glob("*.yaml")):
            w = load_workload(path)
            items.append(DbWorkloadModel(name=w.name, file=path.name, queries=[
                {"id": q.query_id, "kind": q.kind, "weight": q.weight, "sql": q.sql} for q in w.queries
            ]))
        return DbWorkloadsResponse(workloads=items)

    @app.get("/db/snapshots", response_model=DbSnapshotsResponse, dependencies=[Depends(require_token)])
    def get_db_snapshots(target: str | None = None, limit: int = 20) -> DbSnapshotsResponse:
        rows = Journal(cfg).snapshots(target, limit=limit)
        return DbSnapshotsResponse(snapshots=[DbSnapshotModel(**{k: r[k] for k in DbSnapshotModel.model_fields}) for r in rows])

    @app.post("/db/discover", response_model=DbSnapshotModel, dependencies=[Depends(require_token)])
    def post_db_discover(body: DbDiscoverRequest) -> DbSnapshotModel:
        from .advisor import structural_findings

        target = target_or_404(body.target)
        adapter = adapter_for(target)
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

    @app.get("/db/operations", response_model=DbOperationsResponse, dependencies=[Depends(require_token)])
    def get_db_operations(limit: int = 30) -> DbOperationsResponse:
        return DbOperationsResponse(operations=[_operation_model(r) for r in Journal(cfg).recent(limit)], running=running["operation_id"])

    def _artifact_json(detail_artifacts: list[dict], name: str) -> dict:
        for a in detail_artifacts:
            if a["name"] == name and Path(a["path"]).exists():
                return json.loads(Path(a["path"]).read_text(encoding="utf-8"))
        return {}

    @app.get("/db/operations/{operation_id}", response_model=DbOperationDetailResponse, dependencies=[Depends(require_token)])
    def get_db_operation(operation_id: str) -> DbOperationDetailResponse:
        journal = Journal(cfg)
        record = journal.get(operation_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"No operation {operation_id}")
        artifacts = journal.artifacts(operation_id)
        verdicts = _artifact_json(artifacts, "policy_verdicts.json").get("verdicts", {})
        return DbOperationDetailResponse(
            operation=_operation_model(record), request=record.request, decision=record.decision,
            steps=journal.steps(operation_id), changes=journal.changes(operation_id),
            artifacts=[{k: a[k] for k in ("name", "kind", "sha256", "bytes", "created_at")} for a in artifacts],
            candidates=_artifact_json(artifacts, "candidates.json").get("candidates", []),
            verdicts={cid: {"key": v["candidate"]["key"], "eligible": v["eligible"], "blocking": v["blocking"],
                            "checks": v["checks"]} for cid, v in verdicts.items()},
        )

    @app.get("/db/operations/{operation_id}/report", response_class=PlainTextResponse, dependencies=[Depends(require_token)])
    def get_db_operation_report(operation_id: str) -> PlainTextResponse:
        report = next((a for a in Journal(cfg).artifacts(operation_id) if a["name"] == "report.md"), None)
        if report is None:
            raise HTTPException(status_code=404, detail=f"No report for {operation_id} (still running?)")
        return PlainTextResponse(Path(report["path"]).read_text(encoding="utf-8"), media_type="text/markdown")

    @app.get("/db/operations/{operation_id}/evidence", dependencies=[Depends(require_token)])
    def get_db_operation_evidence(operation_id: str) -> StreamingResponse:
        artifacts = Journal(cfg).artifacts(operation_id)
        if not artifacts:
            raise HTTPException(status_code=404, detail=f"No evidence for {operation_id}")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for a in artifacts:
                zf.write(a["path"], arcname=a["name"])
            zf.writestr("MANIFEST.json", json.dumps(
                [{k: a[k] for k in ("name", "kind", "sha256", "bytes", "created_at")} for a in artifacts], indent=2))
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{operation_id}-evidence.zip"'})

    @app.post("/db/operations", response_model=DbOptimizeResponse, status_code=202, dependencies=[Depends(require_token)])
    def post_db_operation(body: DbOptimizeRequest) -> DbOptimizeResponse:
        from .operation import IndexOptimization, OptimizeRequest, new_operation_id
        from .policy import load_policy
        from .workload import load_workload

        target = target_or_404(body.target)
        workload_path = WORKLOADS_DIR / body.workload
        if Path(body.workload).name != body.workload or not workload_path.is_file():
            raise HTTPException(status_code=400, detail=f"Unknown workload {body.workload!r}; see GET /db/workloads")
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail=f"Operation {running['operation_id']} is still running")
        operation_id = new_operation_id()
        running["operation_id"] = operation_id

        def work() -> None:
            adapter = None
            try:
                adapter = adapter_for(target)
                request = OptimizeRequest(target=target.name, workload_path=str(workload_path), candidates=body.candidates,
                                          use_llm=body.use_llm, llm_model=body.llm_model, actor=ACTOR_SERVER)
                IndexOptimization(cfg, target, adapter, load_policy(), load_workload(workload_path), request,
                                  operation_id=operation_id).run()
            finally:
                if adapter is not None:
                    adapter.close()
                running["operation_id"] = None
                lock.release()

        threading.Thread(target=work, name=f"dbeng-{operation_id}", daemon=True).start()
        return DbOptimizeResponse(operation_id=operation_id, status="RUNNING")

    @app.post("/db/recover", response_model=DbRecoverResponse, dependencies=[Depends(require_token)])
    def post_db_recover(body: DbRecoverRequest) -> DbRecoverResponse:
        from .operation import recover
        from .policy import load_policy

        target = target_or_404(body.target)
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail=f"Operation {running['operation_id']} is still running")
        adapter = None
        try:
            adapter = adapter_for(target)
            actions = recover(cfg, target, adapter, load_policy(), actor=ACTOR_SERVER)
        finally:
            if adapter is not None:
                adapter.close()
            lock.release()
        return DbRecoverResponse(target=target.name, actions=actions)
