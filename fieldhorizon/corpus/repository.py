"""
All harvester database access, in one place.

Every state change goes through `transition()`, which validates against
the state machine in models.py *before* writing, and always writes both
the new state and its `corpus_state_events` row in the same transaction.
There is deliberately no "just update the state column" escape hatch:
a state that changed without an event row would break resumability, which
is the one property the whole subsystem is built on.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..db import connect
from .models import (
    PIPELINE_VERSION,
    Candidate,
    Classification,
    Destination,
    IllegalTransition,
    QualityReport,
    RightsDecision,
    State,
    assert_transition,
)

logger = logging.getLogger(__name__)


def utcnow() -> str:
    """ISO-8601 UTC, second precision -- the format every other table uses."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ItemRecord:
    """A read-only view of one corpus_items row, with JSON fields decoded."""

    document_id: str
    source_id: str
    external_id: str
    title: str
    authors: list[str]
    language: str
    state: State
    destination: Destination | None
    normalized_license: str
    distribution_scope: str
    rights_status: str
    quality_score: float | None
    raw_sha256: str | None
    normalized_sha256: str | None
    simhash: str | None
    canonical_url: str
    content_url: str
    materialized_path: str | None
    source_row_id: int | None
    parent_document_id: str | None
    canonical_work_id: str | None
    duplicate_of: str | None
    attribution_required: bool
    share_alike: bool
    rights_verified_at: str | None
    row: dict

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ItemRecord:
        data = dict(row)
        destination = data.get("destination")
        return cls(
            document_id=data["document_id"],
            source_id=data["source_id"],
            external_id=data["external_id"],
            title=data.get("title") or "",
            authors=_loads(data.get("authors"), []),
            language=data.get("language") or "",
            state=State(data["state"]),
            destination=Destination(destination) if destination else None,
            normalized_license=data.get("normalized_license") or "",
            distribution_scope=data.get("distribution_scope") or "UNKNOWN",
            rights_status=data.get("rights_status") or "",
            quality_score=data.get("quality_score"),
            raw_sha256=data.get("raw_sha256"),
            normalized_sha256=data.get("normalized_sha256"),
            simhash=data.get("simhash"),
            canonical_url=data.get("canonical_url") or "",
            content_url=data.get("content_url") or "",
            materialized_path=data.get("materialized_path"),
            source_row_id=data.get("source_row_id"),
            parent_document_id=data.get("parent_document_id"),
            canonical_work_id=data.get("canonical_work_id"),
            duplicate_of=data.get("duplicate_of"),
            attribution_required=bool(data.get("attribution_required")),
            share_alike=bool(data.get("share_alike")),
            rights_verified_at=data.get("rights_verified_at"),
            row=data,
        )


class CorpusRepository:
    """
    Thin, explicit data-access layer over the corpus_* tables.

    One connection is opened per method call (`with connect(...)`), exactly
    as the rest of this repository does -- SQLite is single-writer, and
    holding one long transaction across a whole harvest run is what
    deadlocked ingest_from_manifest against its own event emitter before
    it was fixed.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    # ---------------------------------------------------------------- sources

    def upsert_source(
        self,
        source_id: str,
        adapter: str,
        display_name: str,
        enabled: bool,
        base_url: str,
        trust: float,
        config: dict,
    ) -> None:
        """
        Reconcile the configured source into the operational table without
        clobbering its cursor: the YAML owns configuration, the row owns
        harvest progress.
        """
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO corpus_sources(source_id, adapter, display_name, enabled, base_url, trust, config_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    adapter = excluded.adapter,
                    display_name = excluded.display_name,
                    enabled = excluded.enabled,
                    base_url = excluded.base_url,
                    trust = excluded.trust,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (source_id, adapter, display_name, int(enabled), base_url, float(trust), _json(config), utcnow()),
            )
            conn.commit()

    def get_cursor(self, source_id: str) -> str | None:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT cursor FROM corpus_sources WHERE source_id = ?", (source_id,)).fetchone()
        return row["cursor"] if row else None

    def set_cursor(self, source_id: str, cursor: str | None) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE corpus_sources SET cursor = ?, last_discovery_at = ?, updated_at = ? WHERE source_id = ?",
                (cursor, utcnow(), utcnow(), source_id),
            )
            conn.commit()

    def record_source_outcome(self, source_id: str, ok: bool) -> None:
        with connect(self.db_path) as conn:
            if ok:
                conn.execute(
                    "UPDATE corpus_sources SET last_success_at = ?, consecutive_failures = 0, updated_at = ? "
                    "WHERE source_id = ?",
                    (utcnow(), utcnow(), source_id),
                )
            else:
                conn.execute(
                    "UPDATE corpus_sources SET consecutive_failures = consecutive_failures + 1, updated_at = ? "
                    "WHERE source_id = ?",
                    (utcnow(), source_id),
                )
            conn.commit()

    def list_sources(self) -> list[dict]:
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM corpus_sources ORDER BY source_id").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------- runs

    def start_run(
        self, run_id: str, profile: str, rights_profile: str, mode: str, dry_run: bool, plan_path: str | None = None
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO corpus_runs(
                    run_id, profile, rights_profile, mode, dry_run, started_at, status, pipeline_version, plan_path
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (run_id, profile, rights_profile, mode, int(dry_run), utcnow(), PIPELINE_VERSION, plan_path),
            )
            conn.commit()

    def finish_run(
        self, run_id: str, status: str, stats: dict, report_path: str | None = None, error: str | None = None
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE corpus_runs SET finished_at = ?, status = ?, stats_json = ?, report_path = ?, error_message = ? "
                "WHERE run_id = ?",
                (utcnow(), status, _json(stats), report_path, error, run_id),
            )
            conn.commit()

    def get_run(self, run_id: str) -> dict | None:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM corpus_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def last_run(self) -> dict | None:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM corpus_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def start_source_run(self, run_id: str, source_id: str, cursor_before: str | None) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO corpus_source_runs(run_id, source_id, started_at, status, cursor_before) "
                "VALUES (?, ?, ?, 'running', ?)",
                (run_id, source_id, utcnow(), cursor_before),
            )
            conn.commit()
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def finish_source_run(self, source_run_id: int, status: str, counters: dict, cursor_after: str | None, error: str | None) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE corpus_source_runs SET
                    finished_at = ?, status = ?, discovered = ?, accepted = ?, rejected = ?,
                    quarantined = ?, downloaded = ?, bytes_downloaded = ?, cursor_after = ?, error_message = ?
                WHERE id = ?
                """,
                (
                    utcnow(), status,
                    int(counters.get("discovered", 0)), int(counters.get("accepted", 0)),
                    int(counters.get("rejected", 0)), int(counters.get("quarantined", 0)),
                    int(counters.get("downloaded", 0)), int(counters.get("bytes_downloaded", 0)),
                    cursor_after, error, source_run_id,
                ),
            )
            conn.commit()

    def source_runs_for(self, run_id: str) -> list[dict]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM corpus_source_runs WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- candidates

    def upsert_candidate(self, candidate: Candidate, candidate_id: str, run_id: str) -> bool:
        """
        Insert a newly discovered candidate, or refresh the metadata of one
        already known. Returns True when this was a genuinely new
        discovery -- the counter that distinguishes "the catalogue grew"
        from "we walked the same catalogue again", and the reason a second
        identical run reports 0 new candidates rather than N.

        An existing candidate's `state` and `selection_score` are never
        overwritten here: re-discovering a document already rejected on
        rights grounds must not silently resurrect it.
        """
        raw = _json(candidate.raw_metadata)
        import hashlib

        raw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with connect(self.db_path) as conn:
            existing = conn.execute(
                "SELECT candidate_id FROM corpus_candidates WHERE source_id = ? AND external_id = ?",
                (candidate.source_id, candidate.external_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE corpus_candidates SET
                        title = ?, authors = ?, language = ?, document_type = ?, subjects = ?,
                        canonical_url = ?, download_url = ?, download_format = ?, estimated_bytes = ?,
                        rights_signal_json = ?, raw_metadata_json = ?, raw_metadata_sha256 = ?, updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (
                        candidate.title, _json(list(candidate.authors)), candidate.language,
                        candidate.document_type, _json(list(candidate.subjects)),
                        candidate.canonical_url, candidate.download_url, candidate.download_format,
                        candidate.estimated_bytes, _json(_rights_signal_dict(candidate)), raw, raw_hash,
                        utcnow(), existing["candidate_id"],
                    ),
                )
                conn.commit()
                return False

            conn.execute(
                """
                INSERT INTO corpus_candidates(
                    candidate_id, source_id, external_id, title, authors, language, document_type,
                    subjects, canonical_url, download_url, download_format, estimated_bytes, state,
                    rights_signal_json, raw_metadata_json, raw_metadata_sha256, discovered_at, discovered_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id, candidate.source_id, candidate.external_id, candidate.title,
                    _json(list(candidate.authors)), candidate.language, candidate.document_type,
                    _json(list(candidate.subjects)), candidate.canonical_url, candidate.download_url,
                    candidate.download_format, candidate.estimated_bytes, State.DISCOVERED.value,
                    _json(_rights_signal_dict(candidate)), raw, raw_hash, utcnow(), run_id,
                ),
            )
            conn.execute(
                "INSERT INTO corpus_state_events(candidate_id, old_state, new_state, reason, pipeline_version, run_id, occurred_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?)",
                (candidate_id, State.DISCOVERED.value, "discovered", PIPELINE_VERSION, run_id, utcnow()),
            )
            conn.commit()
            return True

    def get_candidate(self, candidate_id: str) -> dict | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM corpus_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_candidate(self, source_id: str, external_id: str) -> dict | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM corpus_candidates WHERE source_id = ? AND external_id = ?",
                (source_id, external_id),
            ).fetchone()
        return dict(row) if row else None

    def candidates_in_state(self, state: State, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM corpus_candidates WHERE state = ? ORDER BY discovered_at ASC, candidate_id ASC"
        params: list = [state.value]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def set_candidate_state(
        self,
        candidate_id: str,
        new_state: State,
        reason: str = "",
        run_id: str | None = None,
        reason_codes: Sequence[str] = (),
        error: dict | None = None,
    ) -> None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT state FROM corpus_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown candidate {candidate_id!r}")
            old = State(row["state"])
            if old == new_state:
                return
            assert_transition(old, new_state)
            conn.execute(
                "UPDATE corpus_candidates SET state = ?, updated_at = ? WHERE candidate_id = ?",
                (new_state.value, utcnow(), candidate_id),
            )
            conn.execute(
                """
                INSERT INTO corpus_state_events(
                    candidate_id, old_state, new_state, reason, reason_codes, pipeline_version, run_id, error_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id, old.value, new_state.value, reason, _json(list(reason_codes)),
                    PIPELINE_VERSION, run_id, _json(error) if error else None, utcnow(),
                ),
            )
            conn.commit()

    def set_candidate_selection(self, candidate_id: str, score: float, rationale: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE corpus_candidates SET selection_score = ?, selection_rationale = ?, updated_at = ? "
                "WHERE candidate_id = ?",
                (float(score), rationale, utcnow(), candidate_id),
            )
            conn.commit()

    def count_candidates_by_state(self) -> dict[str, int]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM corpus_candidates GROUP BY state"
            ).fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    # ------------------------------------------------------------------ items

    def create_item(self, document_id: str, candidate: Candidate, candidate_id: str, run_id: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO corpus_items(
                    document_id, candidate_id, source_id, external_id, title, authors, contributors,
                    translator, language, publication_date, edition_date, document_type, subjects,
                    canonical_url, content_url, source_adapter, discovered_at, source_format,
                    state, pipeline_version, untrusted_content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    document_id, candidate_id, candidate.source_id, candidate.external_id,
                    candidate.title, _json(list(candidate.authors)), _json(list(candidate.contributors)),
                    candidate.translator, candidate.language, candidate.publication_date,
                    candidate.edition_date, candidate.document_type, _json(list(candidate.subjects)),
                    candidate.canonical_url, candidate.download_url, candidate.source_id,
                    utcnow(), candidate.download_format, State.RIGHTS_PENDING.value, PIPELINE_VERSION,
                ),
            )
            conn.commit()

    def get_item(self, document_id: str) -> ItemRecord | None:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM corpus_items WHERE document_id = ?", (document_id,)).fetchone()
        return ItemRecord.from_row(row) if row else None

    def find_item(self, source_id: str, external_id: str) -> ItemRecord | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM corpus_items WHERE source_id = ? AND external_id = ?",
                (source_id, external_id),
            ).fetchone()
        return ItemRecord.from_row(row) if row else None

    def items_in_states(self, states: Iterable[State], limit: int | None = None) -> list[ItemRecord]:
        state_values = [s.value for s in states]
        if not state_values:
            return []
        placeholders = ",".join("?" for _ in state_values)
        sql = f"SELECT * FROM corpus_items WHERE state IN ({placeholders}) ORDER BY created_at ASC, document_id ASC"
        params: list = list(state_values)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ItemRecord.from_row(r) for r in rows]

    def all_items(self) -> list[ItemRecord]:
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM corpus_items ORDER BY document_id").fetchall()
        return [ItemRecord.from_row(r) for r in rows]

    def count_items_by_state(self) -> dict[str, int]:
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS n FROM corpus_items GROUP BY state").fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    def transition(
        self,
        document_id: str,
        new_state: State,
        reason: str = "",
        run_id: str | None = None,
        reason_codes: Sequence[str] = (),
        error: dict | None = None,
        updates: dict | None = None,
    ) -> None:
        """
        The single way a document's state ever changes.

        `updates` are extra corpus_items column assignments applied in the
        SAME transaction as the state change -- so a document can never be
        observed as NORMALIZED without its normalized_sha256, or as
        MATERIALIZED without its path. Column names come only from
        harvester code, never from harvested data.
        """
        updates = updates or {}
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT state FROM corpus_items WHERE document_id = ?", (document_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown document {document_id!r}")
            old = State(row["state"])
            if old != new_state:
                assert_transition(old, new_state)

            assignments = ["state = ?", "pipeline_version = ?", "updated_at = ?"]
            params: list = [new_state.value, PIPELINE_VERSION, utcnow()]
            for column, value in updates.items():
                if not column.replace("_", "").isalnum():
                    raise ValueError(f"Refusing to build SQL from column name {column!r}")
                assignments.append(f"{column} = ?")
                params.append(value)
            params.append(document_id)
            conn.execute(f"UPDATE corpus_items SET {', '.join(assignments)} WHERE document_id = ?", params)

            conn.execute(
                """
                INSERT INTO corpus_state_events(
                    document_id, old_state, new_state, reason, reason_codes, pipeline_version, run_id, error_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id, old.value, new_state.value, reason, _json(list(reason_codes)),
                    PIPELINE_VERSION, run_id, _json(error) if error else None, utcnow(),
                ),
            )
            conn.commit()

    def update_item(self, document_id: str, **updates) -> None:
        """Column updates that are not themselves a state change."""
        if not updates:
            return
        assignments = []
        params: list = []
        for column, value in updates.items():
            if not column.replace("_", "").isalnum():
                raise ValueError(f"Refusing to build SQL from column name {column!r}")
            assignments.append(f"{column} = ?")
            params.append(value)
        assignments.append("updated_at = ?")
        params.append(utcnow())
        params.append(document_id)
        with connect(self.db_path) as conn:
            conn.execute(f"UPDATE corpus_items SET {', '.join(assignments)} WHERE document_id = ?", params)
            conn.commit()

    def state_events(self, document_id: str) -> list[dict]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM corpus_state_events WHERE document_id = ? ORDER BY id", (document_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- artifacts

    def record_plan(self, document_id: str, plan: dict, run_id: str = "") -> None:
        """
        Persist one document's acquisition plan.

        Upserted on `document_key`: a document re-planned in a later run
        (because a credential appeared, or a provider started hosting
        what it only described before) must show its CURRENT plan, not a
        history of every plan it ever had. The state-event log already
        records the transitions.
        """
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO corpus_acquisition_plans
                    (document_key, document_id, discovered_by, metadata_from,
                     rights_evidence_from, content_hosted_by, fallback_hosts,
                     expected_format, normalization_pipeline, credential_requirement,
                     provider_trust, status, content_url, reason, run_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(document_key) DO UPDATE SET
                    document_id=excluded.document_id,
                    discovered_by=excluded.discovered_by,
                    metadata_from=excluded.metadata_from,
                    rights_evidence_from=excluded.rights_evidence_from,
                    content_hosted_by=excluded.content_hosted_by,
                    fallback_hosts=excluded.fallback_hosts,
                    expected_format=excluded.expected_format,
                    normalization_pipeline=excluded.normalization_pipeline,
                    credential_requirement=excluded.credential_requirement,
                    provider_trust=excluded.provider_trust,
                    status=excluded.status,
                    content_url=excluded.content_url,
                    reason=excluded.reason,
                    run_id=excluded.run_id
                """,
                (
                    plan.get("document_key", document_id), document_id,
                    plan.get("discovered_by", ""), plan.get("metadata_from", ""),
                    plan.get("rights_evidence_from", ""), plan.get("content_hosted_by", ""),
                    json.dumps(plan.get("fallback_hosts", [])),
                    plan.get("expected_format", ""), plan.get("normalization_pipeline", ""),
                    plan.get("credential_requirement", ""),
                    float(plan.get("provider_trust", 0.5) or 0.5),
                    str(plan.get("status", "")), plan.get("content_url", ""),
                    (plan.get("reason") or "")[:500], run_id,
                ),
            )

    def get_plan(self, document_id: str) -> dict | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM corpus_acquisition_plans WHERE document_id = ?", (document_id,)
            ).fetchone()
        if row is None:
            return None
        plan = dict(row)
        plan["fallback_hosts"] = json.loads(plan.get("fallback_hosts") or "[]")
        return plan

    def plans_by_status(self) -> dict[str, int]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM corpus_acquisition_plans GROUP BY status"
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def record_artifact(
        self, document_id: str, kind: str, sha256: str, path: str, size_bytes: int, mime: str = "", encoding: str = ""
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO corpus_artifacts(document_id, kind, sha256, path, size_bytes, mime, encoding)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (document_id, kind, sha256, path, int(size_bytes), mime, encoding),
            )
            conn.commit()

    def artifacts(self, document_id: str, kind: str | None = None) -> list[dict]:
        with connect(self.db_path) as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM corpus_artifacts WHERE document_id = ? AND kind = ? ORDER BY id",
                    (document_id, kind),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM corpus_artifacts WHERE document_id = ? ORDER BY id", (document_id,)
                ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- rights

    def record_rights_decision(
        self,
        decision: RightsDecision,
        document_id: str | None = None,
        candidate_id: str | None = None,
        run_id: str | None = None,
    ) -> int:
        """
        Append a rights verdict. Never updates a previous one: the history
        of what was believed, and on what evidence, is the audit trail.
        """
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO corpus_rights_decisions(
                    document_id, candidate_id, decision, normalized_license, rights_scope,
                    commercial_use, redistribution, derivatives, attribution_required, share_alike,
                    jurisdictions, evidence_json, reason_codes, policy_profile, policy_version,
                    evidence_checked_at, notes, run_id, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id, candidate_id, decision.decision.value, decision.normalized_license,
                    decision.rights_scope.value, int(decision.commercial_use), int(decision.redistribution),
                    int(decision.derivatives), int(decision.attribution_required), int(decision.share_alike),
                    _json(list(decision.jurisdictions)),
                    _json([e.to_dict() for e in decision.evidence]),
                    _json([c.value for c in decision.reason_codes]),
                    decision.policy_profile, decision.policy_version,
                    decision.evidence_checked_at or utcnow(), decision.notes, run_id, utcnow(),
                ),
            )
            conn.commit()
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def latest_rights_decision(self, document_id: str) -> dict | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM corpus_rights_decisions WHERE document_id = ? ORDER BY id DESC LIMIT 1",
                (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def rights_decisions(self, document_id: str) -> list[dict]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM corpus_rights_decisions WHERE document_id = ? ORDER BY id", (document_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------- classification

    def record_classification(self, document_id: str, classification: Classification, run_id: str | None = None) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO corpus_classifications(
                    document_id, destination, manifesto_score, confidence, document_form, primary_domain,
                    secondary_tags, normative_intent, mobilization_intent, doctrinal_intent,
                    narrative_intent, analytical_intent, rationale, evidence_json,
                    classifier_version, classifier_kind, low_confidence, run_id, classified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id, classification.destination.value, classification.manifesto_score,
                    classification.confidence, classification.document_form, classification.primary_domain,
                    _json(list(classification.secondary_tags)), classification.normative_intent,
                    classification.mobilization_intent, classification.doctrinal_intent,
                    classification.narrative_intent, classification.analytical_intent,
                    classification.rationale, _json([dict(e) for e in classification.evidence]),
                    classification.classifier_version, classification.classifier_kind,
                    int(classification.low_confidence), run_id, utcnow(),
                ),
            )
            conn.commit()

    def latest_classification(self, document_id: str) -> dict | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM corpus_classifications WHERE document_id = ? ORDER BY id DESC LIMIT 1",
                (document_id,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------- duplicates

    def record_duplicate(
        self, cluster_id: str, kind: str, representative: str, member: str, similarity: float
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO corpus_duplicate_clusters(cluster_id, kind, representative_document_id) "
                "VALUES (?, ?, ?)",
                (cluster_id, kind, representative),
            )
            conn.execute(
                "INSERT OR IGNORE INTO corpus_duplicate_members(cluster_id, document_id, similarity, is_representative) "
                "VALUES (?, ?, ?, ?)",
                (cluster_id, representative, 1.0, 1),
            )
            conn.execute(
                "INSERT OR IGNORE INTO corpus_duplicate_members(cluster_id, document_id, similarity, is_representative) "
                "VALUES (?, ?, ?, 0)",
                (cluster_id, member, float(similarity)),
            )
            conn.commit()

    def duplicate_clusters(self) -> list[dict]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT c.cluster_id, c.kind, c.representative_document_id,
                       GROUP_CONCAT(m.document_id) AS members
                FROM corpus_duplicate_clusters c
                LEFT JOIN corpus_duplicate_members m ON m.cluster_id = c.cluster_id
                GROUP BY c.cluster_id
                ORDER BY c.cluster_id
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def indexed_fingerprints(self, exclude_document_id: str = "") -> list[dict]:
        """
        (document_id, normalized_sha256, simhash, title, authors, language)
        for every document that already has normalized text -- the corpus
        the deduplicator compares a new arrival against.
        """
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT document_id, normalized_sha256, simhash, title, authors, language,
                       translator, edition_id, canonical_work_id, state, raw_sha256, normalized_chars
                FROM corpus_items
                WHERE normalized_sha256 IS NOT NULL AND document_id != ?
                """,
                (exclude_document_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- attributions

    def record_attribution(self, document_id: str, role: str, name: str, url: str = "", note: str = "") -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO corpus_attributions(document_id, role, name, url, note) VALUES (?, ?, ?, ?, ?)",
                (document_id, role, name, url, note),
            )
            conn.commit()

    def attributions(self, document_id: str) -> list[dict]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM corpus_attributions WHERE document_id = ? ORDER BY id", (document_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- errors

    def record_error(
        self,
        stage: str,
        error_type: str,
        message: str,
        retryable: bool = True,
        document_id: str | None = None,
        candidate_id: str | None = None,
        source_id: str | None = None,
        run_id: str | None = None,
        attempt: int = 1,
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO corpus_errors(
                    document_id, candidate_id, source_id, run_id, stage, error_type, error_message, retryable, attempt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (document_id, candidate_id, source_id, run_id, stage, error_type, message[:2000], int(retryable), attempt),
            )
            conn.commit()

    def errors_for_run(self, run_id: str) -> list[dict]:
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM corpus_errors WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def attempt_count(self, document_id: str, stage: str) -> int:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM corpus_errors WHERE document_id = ? AND stage = ?",
                (document_id, stage),
            ).fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------- http cache

    def get_http_cache(self, url: str) -> dict | None:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM corpus_http_cache WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None

    def set_http_cache(
        self, url: str, etag: str | None, last_modified: str | None, status_code: int, content_sha256: str | None
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO corpus_http_cache(url, etag, last_modified, fetched_at, status_code, content_sha256)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    etag = excluded.etag, last_modified = excluded.last_modified,
                    fetched_at = excluded.fetched_at, status_code = excluded.status_code,
                    content_sha256 = excluded.content_sha256
                """,
                (url, etag, last_modified, utcnow(), status_code, content_sha256),
            )
            conn.commit()

    # -------------------------------------------------------------- reporting

    def total_corpus_bytes(self) -> int:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT COALESCE(SUM(size_bytes), 0) AS n FROM corpus_artifacts").fetchone()
        return int(row["n"]) if row else 0

    def distribution(self, column: str) -> dict[str, int]:
        """
        Counts grouped by one corpus_items column. `column` is validated
        against an allowlist rather than interpolated blind -- this is
        reached from the CLI/report layer, and metadata from harvested
        documents must never reach SQL construction (security rule).
        """
        allowed = {
            "language", "destination", "normalized_license", "source_id",
            "state", "distribution_scope", "document_type", "rights_status",
        }
        if column not in allowed:
            raise ValueError(f"Unsupported distribution column {column!r}")
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT COALESCE({column}, '') AS k, COUNT(*) AS n FROM corpus_items GROUP BY k ORDER BY n DESC"
            ).fetchall()
        return {r["k"]: int(r["n"]) for r in rows}

    def quality_report(self, document_id: str) -> QualityReport | None:
        item = self.get_item(document_id)
        if item is None:
            return None
        data = _loads(item.row.get("quality_report_json"), None)
        if not data:
            return None
        return QualityReport(
            score=float(data.get("score", 0.0)),
            language_detected=data.get("language_detected", ""),
            language_confidence=float(data.get("language_confidence", 0.0)),
            char_count=int(data.get("char_count", 0)),
            word_count=int(data.get("word_count", 0)),
            issues=tuple(data.get("issues", [])),
            metrics=data.get("metrics", {}),
        )


def _rights_signal_dict(candidate: Candidate) -> dict:
    signal = candidate.rights
    return {
        "content_license": signal.content_license,
        "metadata_license": signal.metadata_license,
        "rights_statement_uri": signal.rights_statement_uri,
        "provider_declared_scope": signal.provider_declared_scope,
        "jurisdictions": list(signal.jurisdictions),
        "evidence": [e.to_dict() for e in signal.evidence],
        "access_restricted": signal.access_restricted,
        "borrow_only": signal.borrow_only,
        "uploader_asserted_only": signal.uploader_asserted_only,
        "raw_rights_text": signal.raw_rights_text,
    }


__all__ = ["CorpusRepository", "ItemRecord", "IllegalTransition", "utcnow"]
