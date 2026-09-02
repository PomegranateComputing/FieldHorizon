from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from .config import AppConfig
from .db import connect
from .events import DomainEvent, EventRepository
from .provenance import OBJECT_CYCLE, REL_REHABILITATES, REL_RETIRES, ProvenanceEdge, ProvenanceEdgeRepository
from .schools import ActiveSchool

logger = logging.getLogger(__name__)

CREATION_WRITE_TIME = "write_time"
CREATION_BACKFILL = "backfill"


class TemporalNotFoundError(LookupError):
    pass


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


@dataclass(frozen=True)
class TemporalCanonState:
    cycle_id: int
    verdict: str
    final_score: float | None
    active: bool
    valid_from: str
    recorded_at: str
    source_event_id: str | None = None
    source_edge_id: int | None = None
    creation_method: str = CREATION_WRITE_TIME
    id: int | None = None
    created_at: str | None = None


def _row_to_state(row) -> TemporalCanonState:
    return TemporalCanonState(
        id=int(row["id"]),
        cycle_id=int(row["cycle_id"]),
        verdict=row["verdict"],
        final_score=row["final_score"],
        active=bool(row["active"]),
        valid_from=row["valid_from"],
        recorded_at=row["recorded_at"],
        source_event_id=row["source_event_id"],
        source_edge_id=row["source_edge_id"],
        creation_method=row["creation_method"],
        created_at=row["created_at"],
    )


class TemporalCanonRepository:
    """
    Append-only store for canon_temporal_states, mirroring EventRepository's
    and ProvenanceEdgeRepository's shape. No update/delete method exists:
    each verdict period a cycle passes through is a NEW row, never a
    mutation of a prior one -- see db.py's canon_temporal_states comment
    for why valid_to/superseded_by are deliberately NOT stored columns
    here (they're derived at query time as "the next row for this
    cycle_id", see canon_valid_as_of).
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def append(
        self,
        cycle_id: int,
        verdict: str,
        final_score: float | None,
        active: bool = True,
        valid_from: str | None = None,
        source_event_id: str | None = None,
        source_edge_id: int | None = None,
        creation_method: str = CREATION_WRITE_TIME,
    ) -> TemporalCanonState:
        valid_from = valid_from or _now()
        recorded_at = _now()

        with connect(self.cfg.database) as conn:
            cur = conn.execute(
                """
                INSERT INTO canon_temporal_states(
                    cycle_id, verdict, final_score, active, valid_from, recorded_at,
                    source_event_id, source_edge_id, creation_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_id, verdict, final_score, 1 if active else 0, valid_from, recorded_at,
                    source_event_id, source_edge_id, creation_method,
                ),
            )
            conn.commit()
            assert cur.lastrowid is not None
            state_id = int(cur.lastrowid)

        return TemporalCanonState(
            id=state_id, cycle_id=cycle_id, verdict=verdict, final_score=final_score, active=active,
            valid_from=valid_from, recorded_at=recorded_at, source_event_id=source_event_id,
            source_edge_id=source_edge_id, creation_method=creation_method,
        )

    def history_for_cycle(self, cycle_id: int) -> list[TemporalCanonState]:
        with connect(self.cfg.database) as conn:
            rows = conn.execute(
                "SELECT * FROM canon_temporal_states WHERE cycle_id = ? ORDER BY id ASC", (cycle_id,)
            ).fetchall()
        return [_row_to_state(row) for row in rows]

    def has_any_for_cycle(self, cycle_id: int) -> bool:
        with connect(self.cfg.database) as conn:
            row = conn.execute(
                "SELECT 1 FROM canon_temporal_states WHERE cycle_id = ? LIMIT 1", (cycle_id,)
            ).fetchone()
        return row is not None


def _canon_as_of(cfg: AppConfig, cutoff: str, time_column: str) -> list[int]:
    """
    `time_column` is one of the two internal, hardcoded column names below
    -- never external input -- so interpolating it into the query is safe.
    For each cycle_id, finds its LATEST state (by insertion order) among
    rows with `time_column <= cutoff`, then filters to verdict='CANON' and
    active=1 -- exactly "active canon" reconstructed as it stood at that
    cutoff, whichever clock `time_column` represents.
    """
    assert time_column in ("valid_from", "recorded_at")
    query = f"""
        SELECT cts.cycle_id
        FROM canon_temporal_states cts
        JOIN (
            SELECT cycle_id, MAX(id) AS latest_id
            FROM canon_temporal_states
            WHERE {time_column} <= ?
            GROUP BY cycle_id
        ) latest ON latest.cycle_id = cts.cycle_id AND latest.latest_id = cts.id
        WHERE cts.verdict = 'CANON' AND cts.active = 1
        ORDER BY cts.cycle_id ASC
    """
    with connect(cfg.database) as conn:
        rows = conn.execute(query, (cutoff,)).fetchall()
    return [int(row["cycle_id"]) for row in rows]


def canon_valid_as_of(cfg: AppConfig, timestamp: str) -> list[int]:
    """
    Valid-time travel: which cycle ids were true CANON, in the modeled
    world, as of `timestamp` -- using each state's valid_from. Equal to
    canon_as_of_transaction_time for any normally-written history (this
    system never backdates a live write); they diverge only for a
    backfilled reconstruction, where recorded_at (when the backfill ran)
    postdates valid_from (the historical moment being reconstructed).
    """
    return _canon_as_of(cfg, timestamp, "valid_from")


def canon_as_of_transaction_time(cfg: AppConfig, timestamp: str) -> list[int]:
    """Transaction-time travel: which cycle ids Field Horizon believed were active CANON using only what it had actually RECORDED by `timestamp`."""
    return _canon_as_of(cfg, timestamp, "recorded_at")


def canon_as_of_cycle(cfg: AppConfig, cycle_id: int) -> list[int]:
    """Convenience wrapper: "as of cycle N" reads as "using cycle N's own created_at as the valid-time cutoff"."""
    with connect(cfg.database) as conn:
        row = conn.execute("SELECT created_at FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    if row is None:
        raise TemporalNotFoundError(f"No cycle {cycle_id}")
    return canon_valid_as_of(cfg, row["created_at"])


@dataclass(frozen=True)
class TemporalTransition:
    from_state: TemporalCanonState | None
    to_state: TemporalCanonState
    event: DomainEvent | None
    edge: ProvenanceEdge | None


@dataclass(frozen=True)
class WhyChangedResult:
    cycle_id: int
    transitions: list[TemporalTransition]


def why_changed(cfg: AppConfig, cycle_id: int) -> WhyChangedResult:
    """
    Resolves every verdict transition a cycle has passed through to the
    real domain_events row and provenance_edges row that caused it
    (source_event_id/source_edge_id, populated at write time by
    cycle.py/multicycle.py/council.py) -- not a re-derivation, a lookup of
    linkage already recorded when each state was written.
    """
    history = TemporalCanonRepository(cfg).history_for_cycle(cycle_id)
    events_repo = EventRepository(cfg)
    edges_repo = ProvenanceEdgeRepository(cfg)

    transitions = []
    for index, state in enumerate(history):
        prev = history[index - 1] if index > 0 else None
        event = events_repo.get(state.source_event_id) if state.source_event_id else None
        edge = edges_repo.get_by_id(state.source_edge_id) if state.source_edge_id is not None else None
        transitions.append(TemporalTransition(from_state=prev, to_state=state, event=event, edge=edge))

    return WhyChangedResult(cycle_id=cycle_id, transitions=transitions)


def _domain_event_id_for_canon_event(cfg: AppConfig, canon_event_id: int) -> str | None:
    with connect(cfg.database) as conn:
        row = conn.execute(
            "SELECT event_id FROM domain_events WHERE canon_event_id = ?", (canon_event_id,)
        ).fetchone()
    return row["event_id"] if row is not None else None


@dataclass(frozen=True)
class TemporalBackfillReport:
    cycles_processed: int
    states_written: int
    unresolved_edges: int


def backfill_canon_temporal_states(cfg: AppConfig) -> TemporalBackfillReport:
    """
    One-shot historical reconstruction for cycles that predate this phase.
    Idempotent per cycle: a cycle with ANY existing canon_temporal_states
    rows (whether from a prior backfill or a live write hook) is skipped
    entirely, never re-processed.

    The tricky part: cycles.verdict is overwritten in place by
    set_cycle_verdict at rehabilitation, so it no longer holds a cycle's
    TRUE original verdict once that's happened -- the only surviving
    record of what it originally was is canon_events.detail['old_verdict']
    on that cycle's first REHABILITATED event. Falls back to the current
    cycles.verdict only when no such event exists (nothing ever
    overwrote it, so current IS original).
    """
    repo = TemporalCanonRepository(cfg)
    edges_repo = ProvenanceEdgeRepository(cfg)

    with connect(cfg.database) as conn:
        cycle_rows = conn.execute("SELECT id, verdict, final_score, created_at FROM cycles WHERE dry_run = 0").fetchall()

    cycles_processed = 0
    states_written = 0
    unresolved_edges = 0

    for cycle_row in cycle_rows:
        cycle_id = int(cycle_row["id"])
        if repo.has_any_for_cycle(cycle_id):
            continue
        cycles_processed += 1

        with connect(cfg.database) as conn:
            event_rows = conn.execute(
                "SELECT id, event, detail, created_at FROM canon_events WHERE cycle_id = ? ORDER BY id ASC",
                (cycle_id,),
            ).fetchall()

        first_rehab = next((row for row in event_rows if row["event"] == "REHABILITATED"), None)
        if first_rehab is not None:
            detail = json.loads(first_rehab["detail"]) if first_rehab["detail"] else {}
            initial_verdict = detail.get("old_verdict") or cycle_row["verdict"]
            initial_score = detail.get("old_score", cycle_row["final_score"])
        else:
            initial_verdict = cycle_row["verdict"]
            initial_score = cycle_row["final_score"]

        repo.append(
            cycle_id, initial_verdict, initial_score,
            valid_from=cycle_row["created_at"], creation_method=CREATION_BACKFILL,
        )
        states_written += 1

        for event_row in event_rows:
            if event_row["event"] == "REHABILITATED":
                detail = json.loads(event_row["detail"]) if event_row["detail"] else {}
                edges = edges_repo.edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_REHABILITATES)
                edge_id = edges[0].id if edges else None
                if edge_id is None:
                    unresolved_edges += 1
                repo.append(
                    cycle_id, detail.get("new_verdict") or cycle_row["verdict"], detail.get("new_score"),
                    active=True, valid_from=event_row["created_at"],
                    source_event_id=_domain_event_id_for_canon_event(cfg, int(event_row["id"])),
                    source_edge_id=edge_id, creation_method=CREATION_BACKFILL,
                )
                states_written += 1
            elif event_row["event"] == "RETIRED":
                # retire_cycle never touches cycles.verdict/final_score --
                # the CURRENT stored values are correct here regardless of
                # whether a rehabilitation happened first (retirement is
                # always the terminal transition, see council.py).
                edges = edges_repo.edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_RETIRES)
                edge_id = edges[0].id if edges else None
                if edge_id is None:
                    unresolved_edges += 1
                repo.append(
                    cycle_id, cycle_row["verdict"], cycle_row["final_score"],
                    active=False, valid_from=event_row["created_at"],
                    source_event_id=_domain_event_id_for_canon_event(cfg, int(event_row["id"])),
                    source_edge_id=edge_id, creation_method=CREATION_BACKFILL,
                )
                states_written += 1

    return TemporalBackfillReport(
        cycles_processed=cycles_processed, states_written=states_written, unresolved_edges=unresolved_edges,
    )


def school_membership_as_of(cfg: AppConfig, timestamp: str) -> list[ActiveSchool]:
    """
    The school generation active as of `timestamp`. schools.run_at
    (existing) already marks each clustering generation's start of
    validity, and a generation's school_members rows are fully immutable
    once written (schools.run_schools never updates an old generation) --
    no schema change needed, just schools.latest_schools' own query
    filtered to the latest run_at <= timestamp instead of the global
    latest.
    """
    with connect(cfg.database) as conn:
        row = conn.execute("SELECT MAX(run_at) AS run_at FROM schools WHERE run_at <= ?", (timestamp,)).fetchone()
        run_at = row["run_at"] if row is not None else None
        if run_at is None:
            return []

        rows = conn.execute(
            """
            SELECT s.id, s.name, s.summary, s.previous_school_id, COUNT(m.cycle_id) AS member_count
            FROM schools s
            LEFT JOIN school_members m ON m.school_id = s.id
            WHERE s.run_at = ?
            GROUP BY s.id
            ORDER BY member_count DESC
            """,
            (run_at,),
        ).fetchall()

    return [
        ActiveSchool(
            id=int(row["id"]),
            name=row["name"],
            summary=row["summary"],
            member_count=int(row["member_count"]),
            previous_school_id=row["previous_school_id"],
        )
        for row in rows
    ]


def weather_as_of(cfg: AppConfig, timestamp: str, scope: str = "canon") -> dict[str, float]:
    """
    The most recent weather_readings row per axis, at or before
    `timestamp`, for the given scope. weather_readings is already an
    append-only time series (recorded_at, scope, axis, value) -- this
    needs no new schema, only a historical cutoff over data that already
    exists.
    """
    with connect(cfg.database) as conn:
        rows = conn.execute(
            """
            SELECT wr.axis, wr.value
            FROM weather_readings wr
            JOIN (
                SELECT axis, MAX(id) AS latest_id
                FROM weather_readings
                WHERE scope = ? AND recorded_at <= ?
                GROUP BY axis
            ) latest ON latest.axis = wr.axis AND latest.latest_id = wr.id
            """,
            (scope, timestamp),
        ).fetchall()
    return {row["axis"]: float(row["value"]) for row in rows}
