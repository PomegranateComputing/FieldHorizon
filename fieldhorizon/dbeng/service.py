"""
The DB engineering service inside the Field Horizon server process.

* Operations run in background threads owned by the server: closing a browser
  or losing the UI connection does not touch them.
* Launch is idempotent: a client request id maps to exactly one operation, so
  a double click, a refresh or a retry never starts a second change. One
  operation per target per process here, and one per target across every
  process through the target-side lease (executor.py).
* Controls (pause / resume / cancel) are journaled requests; the operation
  applies them at its next safe point and records the effect.
* At start, every target is reconciled before any new change is accepted on
  it: unfinished state is recovered automatically when the policy allows and
  ownership is proven (mode automatic_startup), otherwise reported as a
  blocking incident with its diagnostic.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..events import ACTOR_SERVER, OperationEmitter
from . import recovery
from . import status as st
from .journal import Journal
from .targets import TargetConfig, TargetConfigError, default_targets_path, load_targets, redact

WORKLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "dbeng_workloads"


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


@dataclass
class Launch:
    operation_id: str
    created: bool
    status: str


def _default_open_adapter(target: TargetConfig):
    from .adapters import open_adapter

    return open_adapter(target)


class DbService:
    def __init__(
        self,
        cfg: AppConfig,
        *,
        open_adapter: Callable[[TargetConfig], Any] | None = None,
        open_executor: Callable[..., Any] | None = None,
        targets_path: Path | None = None,
        policy_path: Path | None = None,
    ):
        self.cfg = cfg
        self.open_adapter = open_adapter or _default_open_adapter
        self.open_executor = open_executor
        self.targets_path = targets_path or default_targets_path(cfg.root)
        self.policy_path = policy_path
        self.journal = Journal(cfg)
        self._lock = threading.Lock()
        self._running: dict[str, str] = {}          # target -> operation_id (this process)
        self._reconciling: set[str] = set()
        self.startup: dict[str, dict] = {}
        self._live_cache: dict[str, tuple[float, dict]] = {}

    # --- configuration -------------------------------------------------------
    def targets(self) -> dict[str, TargetConfig]:
        try:
            return load_targets(self.targets_path)
        except TargetConfigError as exc:
            raise ServiceError(404, "targets_unavailable", str(exc)) from exc

    def target(self, name: str) -> TargetConfig:
        known = self.targets()
        if name not in known:
            raise ServiceError(404, "unknown_target", f"Unknown target {name!r}")
        return known[name]

    def policy(self):
        from .policy import load_policy

        return load_policy(self.policy_path)

    def workload_path(self, name: str) -> Path:
        path = WORKLOADS_DIR / name
        if Path(name).name != name or not path.is_file() or path.suffix != ".yaml":
            raise ServiceError(400, "unknown_workload", f"Unknown workload {name!r}; see GET /db/workloads")
        return path

    def executor_factory(self, target: TargetConfig, policy) -> Callable[[str], Any]:
        def make(operation_id: str):
            if self.open_executor is not None:
                return self.open_executor(target, policy, operation_id=operation_id)
            from .executor import PostgresExecutor

            return PostgresExecutor(target, policy, operation_id=operation_id, journal_ref=str(self.cfg.database),
                                    targets_path=self.targets_path)
        return make

    # --- state ---------------------------------------------------------------------
    def running(self) -> dict[str, str]:
        with self._lock:
            return dict(self._running)

    def reconciling(self) -> set[str]:
        with self._lock:
            return set(self._reconciling)

    # --- launch ---------------------------------------------------------------------
    def start_optimization(self, *, target: str, workload: str, candidates: list[str], use_llm: bool, llm_model: str | None,
                           request_id: str | None, actor: str = ACTOR_SERVER) -> Launch:
        from .operation import IndexOptimization, OptimizeRequest, new_operation_id
        from .workload import load_workload

        body = {"target": target, "workload": workload, "candidates": sorted(candidates), "use_llm": use_llm, "llm_model": llm_model}
        body_sha = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
        if request_id:
            known = self.journal.request_operation(request_id)
            if known is not None:
                if known["body_sha256"] != body_sha:
                    raise ServiceError(422, "request_id_reused", f"request id {request_id} was already used for a different launch")
                record = self.journal.get(known["operation_id"])
                return Launch(known["operation_id"], False, record.status if record else st.RUNNING)
        tgt = self.target(target)
        path = self.workload_path(workload)
        policy = self.policy()
        loaded = load_workload(path)
        with self._lock:
            if request_id:
                # Checked again under the lock: concurrent clicks with one request id resolve to one operation.
                known = self.journal.request_operation(request_id)
                if known is not None:
                    if known["body_sha256"] != body_sha:
                        raise ServiceError(422, "request_id_reused", f"request id {request_id} was already used for a different launch")
                    record = self.journal.get(known["operation_id"])
                    return Launch(known["operation_id"], False, record.status if record else st.RUNNING)
            if target in self._reconciling:
                raise ServiceError(409, "target_reconciling", f"{target} is being reconciled after start-up; retry in a moment")
            if target in self._running:
                raise ServiceError(409, "operation_running", f"Operation {self._running[target]} is still running on {target}")
            operation_id = new_operation_id()
            if request_id and not self.journal.register_request(request_id, operation_id, body_sha):
                known = self.journal.request_operation(request_id)
                assert known is not None
                return Launch(known["operation_id"], False, st.RUNNING)
            self._running[target] = operation_id

        def work() -> None:
            adapter = None
            try:
                adapter = self.open_adapter(tgt)
                request = OptimizeRequest(target=tgt.name, workload_path=str(path), candidates=candidates, use_llm=use_llm,
                                          llm_model=llm_model, actor=actor, request_id=request_id)
                IndexOptimization(self.cfg, tgt, adapter, policy, loaded, request, operation_id=operation_id,
                                  executor_factory=self.executor_factory(tgt, policy)).run()
            except Exception as exc:  # noqa: BLE001 - IndexOptimization.run journals its own failures; this is the adapter opening
                record = self.journal.get(operation_id)
                if record is None:
                    self.journal.create(operation_id, "index_optimization", tgt.name, body, policy.sha256, loaded.sha256)
                if record is None or record.finished_at is None:
                    self.journal.finish(operation_id, st.BLOCKED, redact(f"could not start: {type(exc).__name__}: {exc}"))
                    self.journal.set_runtime(operation_id, cause_code="target_unreachable")
            finally:
                if adapter is not None:
                    adapter.close()
                with self._lock:
                    if self._running.get(target) == operation_id:
                        del self._running[target]

        threading.Thread(target=work, name=f"dbeng-{operation_id}", daemon=True).start()
        return Launch(operation_id, True, st.RUNNING)

    # --- controls ---------------------------------------------------------------------
    def control(self, operation_id: str, action: str, actor: str = ACTOR_SERVER) -> dict:
        record = self.journal.get(operation_id)
        if record is None:
            raise ServiceError(404, "unknown_operation", f"No operation {operation_id}")
        if record.finished_at is not None:
            raise ServiceError(409, "operation_finished", f"{operation_id} already ended with {record.status}; nothing to {action}")
        if record.kind != "index_optimization":
            raise ServiceError(409, "not_controllable", f"{operation_id} is a {record.kind}; only optimizations can be paused or cancelled")
        if action == "pause" and record.status == st.PAUSED:
            raise ServiceError(409, "already_paused", f"{operation_id} is already paused")
        if action == "resume" and record.status != st.PAUSED:
            raise ServiceError(409, "not_paused", f"{operation_id} is not paused")
        if action == "cancel" and record.status == st.CANCELLING:
            raise ServiceError(409, "already_cancelling", f"{operation_id} is already being cancelled")
        requested = self.journal.request_control(operation_id, action, actor)
        OperationEmitter(self.cfg, actor=actor, aggregate_type="db_operation", correlation_id=operation_id).emit(
            st.EVT_CONTROL_REQUESTED, aggregate_id=operation_id, payload={"action": action, "seq": requested["seq"]})
        return {**requested, "semantics": CONTROL_SEMANTICS[action]}

    # --- recovery ------------------------------------------------------------------------
    def detect(self, target_name: str) -> dict:
        tgt = self.target(target_name)
        adapter = self.open_adapter(tgt)
        try:
            return recovery.safe_detect(adapter, self.journal, tgt, self.policy())
        finally:
            adapter.close()

    def recover(self, target_name: str, *, mode: str = recovery.MODE_MANUAL, actor: str = ACTOR_SERVER) -> dict:
        tgt = self.target(target_name)
        with self._lock:
            if target_name in self._running:
                raise ServiceError(409, "operation_running", f"Operation {self._running[target_name]} is running on {target_name}")
            self._running[target_name] = f"recovery:{mode}"
        adapter = None
        try:
            policy = self.policy()
            adapter = self.open_adapter(tgt)
            return recovery.recover(self.cfg, tgt, adapter, policy, self.executor_factory(tgt, policy), mode=mode, actor=actor)
        finally:
            if adapter is not None:
                adapter.close()
            with self._lock:
                self._running.pop(target_name, None)

    def startup_reconcile(self, *, background: bool = True) -> None:
        """Before any new change: detect, and recover when the policy allows it, on every declared target."""
        try:
            names = list(self.targets())
        except ServiceError as exc:
            self.startup["_targets"] = {"status": "unavailable", "detail": exc.detail}
            return
        with self._lock:
            self._reconciling.update(names)

        def work() -> None:
            for name in names:
                started = datetime.now(UTC).isoformat()
                try:
                    policy = self.policy()
                    if policy.recovery_automatic:
                        result = self.recover(name, mode=recovery.MODE_AUTOMATIC_STARTUP) if name not in self._running else None
                        self.startup[name] = {"status": result["status"] if result else "skipped", "run_id": result["run_id"] if result else None,
                                              "message": result["message"] if result else "operation already running",
                                              "mode": recovery.MODE_AUTOMATIC_STARTUP, "started_at": started}
                    else:
                        detection = self.detect(name)
                        self.startup[name] = {"status": "detected_only", "blocking": detection.get("blocking"),
                                              "mode": "detection", "started_at": started}
                except Exception as exc:  # noqa: BLE001 - an unreachable target is reported, never fatal to the server
                    self.startup[name] = {"status": "unreachable", "detail": redact(f"{type(exc).__name__}: {exc}"), "started_at": started}
                finally:
                    with self._lock:
                        self._reconciling.discard(name)

        if background:
            threading.Thread(target=work, name="dbeng-startup-reconcile", daemon=True).start()
        else:
            work()

    # --- live checks (cached briefly: the overview polls) ------------------------------------
    def live(self, target: TargetConfig, max_age: float = 3.0) -> dict:
        cached = self._live_cache.get(target.name)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        result: dict[str, Any] = {"checked_at": datetime.now(UTC).isoformat()}
        adapter = None
        try:
            adapter = self.open_adapter(target)
            ident = adapter.identity()
            policy = self.policy()
            result.update(
                reachable=True, server_version=ident.get("version"), server_version_num=ident.get("version_num"),
                instance_marker_live=ident.get("instance_marker"), system_identifier_live=ident.get("system_identifier"),
                identity_verified=(ident.get("instance_marker") == target.instance_marker
                                   and ident.get("system_identifier") == target.system_identifier
                                   and target.instance_marker is not None),
                in_recovery=ident.get("in_recovery"),
                detection=recovery.detect(adapter, self.journal, target, policy),
                fieldhorizon_indexes=[s.describe() for s in adapter.fieldhorizon_indexes()],
                ledger=adapter.ledger_rows(),
                build_progress=adapter.index_build_progress(),
            )
        except Exception as exc:  # noqa: BLE001 - shown in the UI as "unreachable", with the (redacted) reason
            result.update(reachable=False, detail=redact(f"{type(exc).__name__}: {exc}")[:400])
        finally:
            if adapter is not None:
                adapter.close()
        self._live_cache[target.name] = (time.monotonic(), result)
        return result


CONTROL_SEMANTICS = {
    "pause": "Takes effect at the next safe point (between steps or measurement rounds), never inside a DDL statement. "
             "The target lease is kept while paused, so no other operation can start on this target. "
             "Authorization is re-verified before any later change.",
    "resume": "Continues from the safe point where the operation paused.",
    "cancel": "Takes effect at the next safe point. A change already applied is removed (compensation, confirmed in the catalog). "
              "A DDL statement already running is not interrupted; the cancel applies when it returns. "
              "A cancel arriving after the change was retained has no effect, and the operation says so.",
}
