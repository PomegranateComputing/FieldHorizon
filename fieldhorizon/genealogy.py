from __future__ import annotations

import json

from .config import AppConfig
from .db import connect
from .events import (
    ACTOR_SYSTEM,
    AGGREGATE_CYCLE,
    CANON_EVENT_TYPE_MAP,
    DomainEvent,
    EventRepository,
    new_correlation_id,
)

# canon_events.event values. A cycle's full history is the append-only
# sequence of these rows against it -- nothing here ever overwrites or
# deletes a prior event.
EVENT_PROMOTED = "PROMOTED"
EVENT_RETIRED = "RETIRED"
EVENT_REHABILITATED = "REHABILITATED"
EVENT_COUNCIL_UPHELD = "COUNCIL_UPHELD"
EVENT_COUNCIL_OVERTURNED = "COUNCIL_OVERTURNED"


def record_canon_event(
    cfg: AppConfig,
    cycle_id: int,
    event: str,
    detail: dict | None = None,
    actor: str = ACTOR_SYSTEM,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> DomainEvent | None:
    """
    Writes the canon_events row exactly as before (schema and vocabulary
    unchanged), then cross-emits a matching domain_events row via
    CANON_EVENT_TYPE_MAP, with canon_event_id pointing back at the row
    just written -- this is the ONE place that cross-reference happens
    (Implementation Brief III, Phase A step 3), so every one of this
    function's callers (cycle.py, multicycle.py, council.py) gets it for
    free. Returns the created DomainEvent so a caller with its own
    OperationEmitter can `.adopt(event.event_id)` to keep its causation
    chain pointing at the true most recent event; returns None if `event`
    has no domain_events mapping (there is none currently, but the
    interface stays honest about that possibility).

    `correlation_id`/`causation_id` are optional: a caller mid-operation
    (cycle.py during CycleStarted..CycleCompleted) passes its own, so this
    cross-emission joins that same chain rather than starting a new one;
    a caller with no active chain gets a fresh standalone correlation_id.
    """
    with connect(cfg.database) as conn:
        cur = conn.execute(
            "INSERT INTO canon_events(cycle_id, event, detail) VALUES (?, ?, ?)",
            (cycle_id, event, json.dumps(detail or {}, ensure_ascii=False)),
        )
        conn.commit()
        canon_event_id = cur.lastrowid

    domain_event_type = CANON_EVENT_TYPE_MAP.get(event)
    if domain_event_type is None:
        return None

    return EventRepository(cfg).append(
        event_type=domain_event_type,
        actor=actor,
        aggregate_type=AGGREGATE_CYCLE,
        correlation_id=correlation_id or new_correlation_id(),
        causation_id=causation_id,
        aggregate_id=cycle_id,
        canon_event_id=canon_event_id,
        payload=detail or {},
    )


def load_canon_events(cfg: AppConfig, cycle_id: int) -> list[dict]:
    with connect(cfg.database) as conn:
        rows = conn.execute(
            "SELECT event, detail, created_at FROM canon_events WHERE cycle_id = ? ORDER BY id ASC",
            (cycle_id,),
        ).fetchall()

    events = []
    for row in rows:
        try:
            detail = json.loads(row["detail"]) if row["detail"] else {}
        except json.JSONDecodeError:
            detail = {}
        events.append({"event": row["event"], "detail": detail, "created_at": row["created_at"]})

    return events


def set_cycle_verdict(cfg: AppConfig, cycle_id: int, verdict: str, final_score: float) -> None:
    """
    Updates a cycle's verdict/final_score after a council re-evaluation --
    the stored prompt/response/fragment text is never touched, only the
    judgment against it. This is what makes canon temporal: the same
    fragment can carry a different verdict at different points in time,
    and canon_events is the record of when and why it changed.
    """
    with connect(cfg.database) as conn:
        conn.execute(
            "UPDATE cycles SET verdict = ?, final_score = ? WHERE id = ?",
            (verdict, final_score, cycle_id),
        )
        conn.commit()


def retire_cycle(cfg: AppConfig, cycle_id: int, reason: str) -> None:
    """
    Marks a cycle retired. Never deletes it: retirement only excludes the
    cycle from future MMR canon selection (canon._load_canon_pool filters
    on retired_at IS NULL) -- it stays fully present in cycle_sources,
    canon_events, and `field-horizon lineage`'s genealogy walk, marked as
    retired there.
    """
    with connect(cfg.database) as conn:
        conn.execute(
            "UPDATE cycles SET retired_at = CURRENT_TIMESTAMP, retirement_reason = ? WHERE id = ?",
            (reason, cycle_id),
        )
        conn.commit()


def parent_cycle_ids_for(canon_rows: list[dict]) -> list[int]:
    """
    The cycle ids whose canon fragments were MMR-selected into a prompt --
    this cycle's doctrinal ancestry, recorded on cycles.parent_cycle_ids
    at write time (cycle.py / multicycle.py).
    """
    return [row["cycle_id"] for row in canon_rows if "cycle_id" in row]
