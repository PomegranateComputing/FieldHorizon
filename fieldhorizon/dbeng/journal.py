"""
The durable record of every DB engineering operation, in additive dbeng_*
tables of the Field Horizon database (the corpus harvester's convention:
CREATE TABLE IF NOT EXISTS, no existing table altered).

Holds Field Horizon's own state -- what was decided, when, on what
evidence -- never a target database's business data. Large evidence
(snapshots, plans, samples) lives in files under outputs/dbeng/, each
registered here with its sha256 so a report can be checked against it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config import AppConfig
from ..db import connect

DBENG_SCHEMA = """
CREATE TABLE IF NOT EXISTS dbeng_operations (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    state TEXT NOT NULL,
    request_json TEXT NOT NULL,
    policy_sha256 TEXT,
    workload_sha256 TEXT,
    decision_json TEXT,
    message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS dbeng_steps (
    operation_id TEXT NOT NULL REFERENCES dbeng_operations(operation_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    step TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'completed', 'failed', 'skipped')),
    detail_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (operation_id, seq)
);

-- The change ledger recovery reads: what was (being) done to the target,
-- and how to undo it. state moves planned -> applying -> applied ->
-- (retained | reverting -> reverted) or -> failed.
CREATE TABLE IF NOT EXISTS dbeng_changes (
    operation_id TEXT NOT NULL REFERENCES dbeng_operations(operation_id) ON DELETE CASCADE,
    change_id TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    object_schema TEXT NOT NULL,
    object_name TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, change_id)
);

CREATE TABLE IF NOT EXISTS dbeng_artifacts (
    operation_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, name)
);

CREATE TABLE IF NOT EXISTS dbeng_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    server_version TEXT,
    system_identifier TEXT,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    summary_json TEXT NOT NULL
);
"""

TERMINAL_STATUSES = {"RETAINED", "REVERTED", "REJECTED", "BLOCKED", "FAILED_RECOVERED", "RECOVERED_REVERTED", "NO_CANDIDATE", "COMPLETED"}


def init_dbeng_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DBENG_SCHEMA)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def evidence_dir(cfg: AppConfig, operation_id: str) -> Path:
    return cfg.outputs / "dbeng" / "operations" / operation_id


@dataclass
class OperationRecord:
    operation_id: str
    kind: str
    target: str
    status: str
    state: str
    request: dict
    policy_sha256: str | None
    workload_sha256: str | None
    decision: dict | None
    message: str | None
    started_at: str
    finished_at: str | None


class Journal:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        with connect(cfg.database) as conn:
            init_dbeng_schema(conn)
            conn.commit()

    # --- operations ------------------------------------------------------
    def create(self, operation_id: str, kind: str, target: str, request: dict, policy_sha256: str, workload_sha256: str | None) -> None:
        with connect(self.cfg.database) as conn:
            conn.execute(
                "INSERT INTO dbeng_operations(operation_id, kind, target, status, state, request_json, policy_sha256, "
                "workload_sha256, started_at) VALUES (?, ?, ?, 'RUNNING', 'created', ?, ?, ?, ?)",
                (operation_id, kind, target, json.dumps(request, sort_keys=True), policy_sha256, workload_sha256, _now()),
            )
            conn.commit()

    def set_state(self, operation_id: str, state: str) -> None:
        with connect(self.cfg.database) as conn:
            conn.execute("UPDATE dbeng_operations SET state = ? WHERE operation_id = ?", (state, operation_id))
            conn.commit()

    def finish(self, operation_id: str, status: str, message: str, decision: dict | None = None) -> None:
        with connect(self.cfg.database) as conn:
            conn.execute(
                "UPDATE dbeng_operations SET status = ?, state = 'finished', message = ?, decision_json = COALESCE(?, decision_json), "
                "finished_at = ? WHERE operation_id = ?",
                (status, message, json.dumps(decision, sort_keys=True) if decision is not None else None, _now(), operation_id),
            )
            conn.commit()

    def record_decision(self, operation_id: str, decision: dict) -> None:
        with connect(self.cfg.database) as conn:
            conn.execute("UPDATE dbeng_operations SET decision_json = ? WHERE operation_id = ?",
                         (json.dumps(decision, sort_keys=True), operation_id))
            conn.commit()

    def get(self, operation_id: str) -> OperationRecord | None:
        with connect(self.cfg.database) as conn:
            row = conn.execute("SELECT * FROM dbeng_operations WHERE operation_id = ?", (operation_id,)).fetchone()
        return _record(row) if row else None

    def recent(self, limit: int = 50) -> list[OperationRecord]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute("SELECT * FROM dbeng_operations ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [_record(r) for r in rows]

    def unfinished(self) -> list[OperationRecord]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute("SELECT * FROM dbeng_operations WHERE finished_at IS NULL ORDER BY started_at").fetchall()
        return [_record(r) for r in rows]

    # --- steps -------------------------------------------------------------
    def step_started(self, operation_id: str, step: str, detail: dict | None = None) -> int:
        with connect(self.cfg.database) as conn:
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM dbeng_steps WHERE operation_id = ?", (operation_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO dbeng_steps(operation_id, seq, step, status, detail_json, started_at) VALUES (?, ?, ?, 'started', ?, ?)",
                (operation_id, seq, step, json.dumps(detail or {}, sort_keys=True, default=str), _now()),
            )
            conn.execute("UPDATE dbeng_operations SET state = ? WHERE operation_id = ?", (step, operation_id))
            conn.commit()
        return int(seq)

    def step_finished(self, operation_id: str, seq: int, status: str, detail: dict | None = None) -> None:
        with connect(self.cfg.database) as conn:
            conn.execute(
                "UPDATE dbeng_steps SET status = ?, detail_json = ?, finished_at = ? WHERE operation_id = ? AND seq = ?",
                (status, json.dumps(detail or {}, sort_keys=True, default=str), _now(), operation_id, seq),
            )
            conn.commit()

    def steps(self, operation_id: str) -> list[dict]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute("SELECT * FROM dbeng_steps WHERE operation_id = ? ORDER BY seq", (operation_id,)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]

    # --- change ledger -------------------------------------------------------
    def change_planned(self, operation_id: str, change_id: str, candidate: dict, schema: str, name: str) -> None:
        with connect(self.cfg.database) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO dbeng_changes(operation_id, change_id, candidate_json, object_schema, object_name, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'planned', ?)",
                (operation_id, change_id, json.dumps(candidate, sort_keys=True), schema, name, _now()),
            )
            conn.commit()

    def change_state(self, operation_id: str, change_id: str, state: str) -> None:
        with connect(self.cfg.database) as conn:
            conn.execute("UPDATE dbeng_changes SET state = ?, updated_at = ? WHERE operation_id = ? AND change_id = ?",
                         (state, _now(), operation_id, change_id))
            conn.commit()

    def changes(self, operation_id: str) -> list[dict]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute("SELECT * FROM dbeng_changes WHERE operation_id = ? ORDER BY change_id", (operation_id,)).fetchall()
        return [{**dict(r), "candidate": json.loads(r["candidate_json"])} for r in rows]

    # --- artifacts -----------------------------------------------------------
    def write_artifact(self, operation_id: str, name: str, kind: str, content: object) -> Path:
        directory = evidence_dir(self.cfg, operation_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        text = content if isinstance(content, str) else json.dumps(content, indent=2, sort_keys=True, default=str, ensure_ascii=False)
        data = text.encode("utf-8")
        path.write_bytes(data)
        with connect(self.cfg.database) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO dbeng_artifacts(operation_id, name, kind, path, sha256, bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (operation_id, name, kind, str(path), hashlib.sha256(data).hexdigest(), len(data), _now()),
            )
            conn.commit()
        return path

    def artifacts(self, operation_id: str) -> list[dict]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute("SELECT * FROM dbeng_artifacts WHERE operation_id = ? ORDER BY created_at, name", (operation_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- snapshots ------------------------------------------------------------
    def record_snapshot(self, target: str, snapshot: dict) -> dict:
        body = json.dumps(snapshot, indent=2, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        snapshot_id = f"S-{snapshot['captured_at'][:19].replace(':', '').replace('-', '')}-{digest[:8]}"
        directory = self.cfg.outputs / "dbeng" / "snapshots" / target
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{snapshot_id}.json"
        path.write_bytes(body)
        summary = {
            "tables": len(snapshot.get("tables", [])),
            "indexes": len(snapshot.get("indexes", [])),
            "constraints": len(snapshot.get("constraints", [])),
            "findings": len(snapshot.get("findings", [])),
            "availability": {k: v.get("status") for k, v in snapshot.get("availability", {}).items()},
            "finding_list": [{k: f.get(k) for k in ("kind", "object", "detail")} for f in snapshot.get("findings", [])],
        }
        server = snapshot.get("server", {})
        with connect(self.cfg.database) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO dbeng_snapshots(snapshot_id, target, captured_at, server_version, system_identifier, sha256, path, summary_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (snapshot_id, target, snapshot["captured_at"], server.get("version_num"), server.get("system_identifier"),
                 digest, str(path), json.dumps(summary, sort_keys=True)),
            )
            conn.commit()
        return {"snapshot_id": snapshot_id, "sha256": digest, "path": str(path), "summary": summary}

    def snapshots(self, target: str | None = None, limit: int = 20) -> list[dict]:
        with connect(self.cfg.database) as conn:
            if target:
                rows = conn.execute("SELECT * FROM dbeng_snapshots WHERE target = ? ORDER BY captured_at DESC LIMIT ?", (target, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM dbeng_snapshots ORDER BY captured_at DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "summary": json.loads(r["summary_json"])} for r in rows]

    def snapshot(self, snapshot_id: str) -> dict | None:
        with connect(self.cfg.database) as conn:
            row = conn.execute("SELECT * FROM dbeng_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "summary": json.loads(row["summary_json"]), "document": json.loads(Path(row["path"]).read_text(encoding="utf-8"))}


def _record(row) -> OperationRecord:
    return OperationRecord(
        operation_id=row["operation_id"], kind=row["kind"], target=row["target"], status=row["status"], state=row["state"],
        request=json.loads(row["request_json"]), policy_sha256=row["policy_sha256"], workload_sha256=row["workload_sha256"],
        decision=json.loads(row["decision_json"]) if row["decision_json"] else None, message=row["message"],
        started_at=row["started_at"], finished_at=row["finished_at"],
    )
