from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .db import connect
from .principal import LOCAL_ADMIN_PRINCIPAL, PrincipalContext

# Actor values: a coarse "which entry point triggered this" tag, not an
# identity or authorization concept (see the optional PrincipalContext
# seam, if ever built, for that). A single-user local system has no
# multi-principal model to justify more than this.
ACTOR_CLI = "cli"
ACTOR_SERVER = "server"
ACTOR_DREAM = "dream"
ACTOR_SYSTEM = "system"

# Aggregate types domain_events currently references.
AGGREGATE_CYCLE = "cycle"
AGGREGATE_COUNCIL = "council"
AGGREGATE_DREAM_RUN = "dream_run"
AGGREGATE_SCHOOL = "school"
AGGREGATE_RETRIEVAL_PLAN = "retrieval_plan"
AGGREGATE_INGEST = "ingest"
AGGREGATE_PROPOSAL = "proposal"

# Cycle chain (fieldhorizon.cycle / fieldhorizon.multicycle). CycleRequested
# and CycleStarted are deliberately collapsed into one event: nothing in
# this synchronous, unqueued system distinguishes "requested" from
# "started". FragmentPromoted fires only on verdict == CANON;
# EvaluationCompleted.payload["verdict"] carries the full four-way verdict
# (CANON/USEFUL_FRAGMENT/HERESY/NOISE) rather than forcing a binary
# promoted/rejected split that doesn't fit this system's verdict space.
EVT_CYCLE_STARTED = "CycleStarted"
EVT_RETRIEVAL_COMPLETED = "RetrievalCompleted"
EVT_AGENT_COMPLETED = "AgentCompleted"
EVT_EVALUATION_COMPLETED = "EvaluationCompleted"
EVT_FRAGMENT_PROMOTED = "FragmentPromoted"
EVT_CYCLE_COMPLETED = "CycleCompleted"
EVT_CYCLE_FAILED = "CycleFailed"

# Council chain (fieldhorizon.council). The last four mirror
# genealogy.py's canon_events vocabulary exactly -- see
# CANON_EVENT_TYPE_MAP below, used by record_canon_event to cross-emit a
# matching domain_events row without altering canon_events itself.
EVT_COUNCIL_STARTED = "CouncilStarted"
EVT_HERESY_TRIAL_COMPLETED = "HeresyTrialCompleted"
EVT_CANON_AUDIT_COMPLETED = "CanonAuditCompleted"
EVT_COUNCIL_COMPLETED = "CouncilCompleted"
EVT_COUNCIL_FAILED = "CouncilFailed"
EVT_FRAGMENT_RETIRED = "FragmentRetired"
EVT_FRAGMENT_REHABILITATED = "FragmentRehabilitated"
EVT_COUNCIL_UPHELD = "CouncilUpheld"
EVT_COUNCIL_OVERTURNED = "CouncilOverturned"

# Dream chain (fieldhorizon.dream).
EVT_DREAM_STARTED = "DreamStarted"
EVT_REGION_SELECTED = "RegionSelected"
EVT_DREAM_PROPOSAL_EMITTED = "DreamProposalEmitted"
EVT_DREAM_COMPLETED = "DreamCompleted"
EVT_DREAM_FAILED = "DreamFailed"

# Ontology proposal review (fieldhorizon.proposals, Implementation Brief IV
# Phase UI-6 item 1). Apply and reject are this app's two clearest
# destructive actions (apply merges into ontology.yaml and git-commits;
# reject removes a proposal from the pending queue) -- FABLE Sec.14 requires
# both confirmed AND event-logged; neither emitted anything before this.
EVT_PROPOSAL_APPLIED = "ProposalApplied"
EVT_PROPOSAL_REJECTED = "ProposalRejected"
EVT_PROPOSAL_APPLY_FAILED = "ProposalApplyFailed"

# Manifest ingest (fieldhorizon.ingest.ingest_from_manifest, Implementation
# Brief IV Phase UI-4 item 2). One event per manifest entry processed
# (IngestSourceCompleted) so a client watching GET /events/stream?run_id=
# sees real per-source progress, not a fabricated percentage.
EVT_INGEST_STARTED = "IngestStarted"
EVT_INGEST_SOURCE_COMPLETED = "IngestSourceCompleted"
EVT_INGEST_COMPLETED = "IngestCompleted"
EVT_INGEST_FAILED = "IngestFailed"

# Schools clustering (fieldhorizon.schools.run_schools, Implementation Brief
# IV Phase UI-5 item 2). AGGREGATE_SCHOOL already existed as a provenance
# object_type; these are its first domain events. One SchoolNamed event per
# cluster named (each is its own LLM call, the slow step), so a client
# watching GET /events/stream?run_id= sees real per-cluster progress.
EVT_SCHOOLS_STARTED = "SchoolsStarted"
EVT_SCHOOL_NAMED = "SchoolNamed"
EVT_SCHOOLS_COMPLETED = "SchoolsCompleted"
EVT_SCHOOLS_FAILED = "SchoolsFailed"

# Retrieval planner v3 (fieldhorizon.retrieval_plan, Implementation Brief
# III, Phase E). EVT_RETRIEVAL_COMPLETED above is reused unchanged at its
# existing cycle.py/multicycle.py call sites; the planner's own emission
# of it (a new, separate call site under its own correlation chain) never
# collides with those.
EVT_RETRIEVAL_PLANNED = "RetrievalPlanned"

# genealogy.EVENT_* -> the domain_events event_type it cross-emits as.
CANON_EVENT_TYPE_MAP = {
    "PROMOTED": EVT_FRAGMENT_PROMOTED,
    "RETIRED": EVT_FRAGMENT_RETIRED,
    "REHABILITATED": EVT_FRAGMENT_REHABILITATED,
    "COUNCIL_UPHELD": EVT_COUNCIL_UPHELD,
    "COUNCIL_OVERTURNED": EVT_COUNCIL_OVERTURNED,
}


@dataclass(frozen=True)
class DomainEvent:
    event_id: str
    event_type: str
    event_version: int
    occurred_at: str
    recorded_at: str
    actor: str
    aggregate_type: str
    aggregate_id: str | None
    correlation_id: str
    causation_id: str | None
    run_id: str
    payload: dict
    id: int | None = None
    canon_event_id: int | None = None
    principal_id: str | None = None


def new_correlation_id() -> str:
    """A fresh id for one user-initiated operation's whole event chain."""
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def _row_to_event(row) -> DomainEvent:
    return DomainEvent(
        id=int(row["id"]),
        event_id=row["event_id"],
        event_type=row["event_type"],
        event_version=int(row["event_version"]),
        occurred_at=row["occurred_at"],
        recorded_at=row["recorded_at"],
        actor=row["actor"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        run_id=row["run_id"],
        canon_event_id=row["canon_event_id"],
        payload=json.loads(row["payload"]),
        principal_id=row["principal_id"],
    )


class EventRepository:
    """
    Append-only store for domain_events. No update or delete method
    exists on this class, by design: domain_events is a history, not a
    mutable projection, and the DB tables it references (cycles,
    canon_events, councils) remain the operational source of truth --
    this repository only records the story around them.
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def append(
        self,
        event_type: str,
        actor: str,
        aggregate_type: str,
        correlation_id: str,
        run_id: str | None = None,
        aggregate_id: str | int | None = None,
        causation_id: str | None = None,
        payload: dict | None = None,
        canon_event_id: int | None = None,
        event_version: int = 1,
        occurred_at: str | None = None,
        principal_id: str | None = None,
    ) -> DomainEvent:
        event_id = str(uuid.uuid4())
        occurred_at = occurred_at or _now()
        recorded_at = _now()
        run_id = run_id or correlation_id
        aggregate_id_str = str(aggregate_id) if aggregate_id is not None else None
        payload = payload or {}

        with connect(self.cfg.database) as conn:
            conn.execute(
                """
                INSERT INTO domain_events(
                    event_id, event_type, event_version, occurred_at, recorded_at, actor,
                    aggregate_type, aggregate_id, correlation_id, causation_id, run_id,
                    canon_event_id, payload, principal_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id, event_type, event_version, occurred_at, recorded_at, actor,
                    aggregate_type, aggregate_id_str, correlation_id, causation_id, run_id,
                    canon_event_id, json.dumps(payload, ensure_ascii=False), principal_id,
                ),
            )
            conn.commit()

        return DomainEvent(
            event_id=event_id, event_type=event_type, event_version=event_version,
            occurred_at=occurred_at, recorded_at=recorded_at, actor=actor,
            aggregate_type=aggregate_type, aggregate_id=aggregate_id_str,
            correlation_id=correlation_id, causation_id=causation_id, run_id=run_id,
            principal_id=principal_id,
            payload=payload, canon_event_id=canon_event_id,
        )

    def get(self, event_id: str) -> DomainEvent | None:
        with connect(self.cfg.database) as conn:
            row = conn.execute("SELECT * FROM domain_events WHERE event_id = ?", (event_id,)).fetchone()
        return _row_to_event(row) if row is not None else None

    def by_correlation(self, correlation_id: str) -> list[DomainEvent]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute(
                "SELECT * FROM domain_events WHERE correlation_id = ? ORDER BY id ASC", (correlation_id,)
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def by_run(self, run_id: str) -> list[DomainEvent]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute("SELECT * FROM domain_events WHERE run_id = ? ORDER BY id ASC", (run_id,)).fetchall()
        return [_row_to_event(row) for row in rows]

    def by_aggregate(self, aggregate_type: str, aggregate_id: str | int) -> list[DomainEvent]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute(
                "SELECT * FROM domain_events WHERE aggregate_type = ? AND aggregate_id = ? ORDER BY id ASC",
                (aggregate_type, str(aggregate_id)),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def recent(self, limit: int = 50) -> list[DomainEvent]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute("SELECT * FROM domain_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_event(row) for row in rows]


def query_events(
    cfg: AppConfig,
    run_id: str | None = None,
    aggregate_id: str | int | None = None,
    event_type: str | None = None,
    since_id: int | None = None,
    limit: int = 200,
) -> list[DomainEvent]:
    """
    A single dynamic-filter query backing both the SSE stream (Implementation
    Brief IV, Phase UI-2) and any future CLI/UI event browsing that needs
    more than one filter at once -- EventRepository's own by_run/by_aggregate/
    recent stay as they are (simple, single-purpose, already used elsewhere)
    rather than being rewritten to share this. `since_id` filters on
    domain_events.id, not a timestamp: ids are monotonic and gap-free for
    this append-only table, so "give me everything after the last one I
    saw" is exact with an id cursor and would only be approximate with a
    recorded_at comparison (two events can share a timestamp at this
    system's write granularity).
    """
    clauses: list[str] = []
    params: list[object] = []

    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if aggregate_id is not None:
        clauses.append("aggregate_id = ?")
        params.append(str(aggregate_id))
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if since_id is not None:
        clauses.append("id > ?")
        params.append(since_id)

    query = "SELECT * FROM domain_events"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id ASC LIMIT ?"
    params.append(limit)

    with connect(cfg.database) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_event(row) for row in rows]


class OperationEmitter:
    """
    Tracks one operation's correlation_id and causation chain so callers
    (cycle.py, multicycle.py, council.py, dream.py) don't thread a
    `last_event_id` variable through by hand. `adopt` registers an event
    created OUTSIDE this emitter -- specifically, genealogy.record_canon_event's
    cross-emission into domain_events when it writes a canon_events row --
    as the new causation pointer, so the next emit() call chains from the
    true most recent event instead of silently skipping it (which would
    otherwise happen: record_canon_event's cross-emitted FragmentPromoted
    would be invisible to this emitter's own causation tracking).
    """

    def __init__(
        self,
        cfg: AppConfig,
        actor: str,
        aggregate_type: str,
        correlation_id: str | None = None,
        principal: PrincipalContext | None = None,
    ):
        self.cfg = cfg
        self.actor = actor
        self.aggregate_type = aggregate_type
        self.correlation_id = correlation_id or new_correlation_id()
        self.principal = principal or LOCAL_ADMIN_PRINCIPAL
        self.repo = EventRepository(cfg)
        self.last_event_id: str | None = None

    def emit(self, event_type: str, aggregate_id: str | int | None = None, payload: dict | None = None) -> DomainEvent:
        event = self.repo.append(
            event_type=event_type,
            actor=self.actor,
            aggregate_type=self.aggregate_type,
            correlation_id=self.correlation_id,
            aggregate_id=aggregate_id,
            causation_id=self.last_event_id,
            payload=payload or {},
            principal_id=self.principal.principal_id,
        )
        self.last_event_id = event.event_id
        return event

    def adopt(self, event_id: str) -> None:
        self.last_event_id = event_id


def export_jsonl(cfg: AppConfig, path: Path, since: str | None = None) -> int:
    """
    On-demand export of domain_events to a JSONL file -- not a live
    dual-write sink, so there is exactly one write path (the DB insert in
    EventRepository.append) and no risk of the export drifting from it.
    Returns the number of rows written.
    """
    query = "SELECT * FROM domain_events"
    params: tuple = ()
    if since:
        query += " WHERE recorded_at >= ?"
        params = (since,)
    query += " ORDER BY id ASC"

    with connect(cfg.database) as conn:
        rows = conn.execute(query, params).fetchall()

    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            event = _row_to_event(row)
            fh.write(json.dumps(event.__dict__, ensure_ascii=False) + "\n")
            count += 1

    return count
