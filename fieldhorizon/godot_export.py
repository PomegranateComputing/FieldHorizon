from __future__ import annotations

import json
from pathlib import Path

from .config import AppConfig
from .contracts import (
    SCHEMA_VERSION,
    BeliefModel,
    BeliefsResponse,
    ContradictionModel,
    ContradictionsResponse,
    DoctrineModel,
    DoctrinesResponse,
    FactionModel,
    FactionsResponse,
)
from .db import connect
from .engine import FieldHorizonEngine
from .ontology import build_pressure_edges, load_nodes
from .schools import school_member_cycle_ids
from .weather import school_weather_profile

BELIEF_SAMPLE_SIZE = 3
CONTRADICTION_LIMIT = 20


def _lineage_root_ids(cfg: AppConfig, cycle_id: int) -> list[int]:
    """Walks parent_cycle_ids back to the cycle(s) with no recorded ancestors."""
    visited: set[int] = set()
    roots: set[int] = set()
    stack = [cycle_id]

    while stack:
        cid = stack.pop()
        if cid in visited:
            continue
        visited.add(cid)

        with connect(cfg.database) as conn:
            row = conn.execute("SELECT parent_cycle_ids FROM cycles WHERE id = ?", (cid,)).fetchone()
        parent_ids = json.loads(row["parent_cycle_ids"]) if row and row["parent_cycle_ids"] else []

        if not parent_ids:
            roots.add(cid)
        else:
            stack.extend(pid for pid in parent_ids if pid not in visited)

    return sorted(roots)


def build_factions(engine: FieldHorizonEngine) -> FactionsResponse:
    """Schools of thought, reframed for a narrative engine: name, doctrine summary, size, and a per-school weather profile."""
    schools = engine.schools()
    factions = []
    for school in schools:
        member_ids = school_member_cycle_ids(engine.cfg, school.id)
        factions.append(
            FactionModel(
                name=school.name,
                doctrine_summary=school.summary,
                size=school.member_count,
                weather_profile=school_weather_profile(engine.cfg, member_ids),
            )
        )
    return FactionsResponse(factions=factions)


def build_doctrines(engine: FieldHorizonEngine) -> DoctrinesResponse:
    """Active canon as doctrine statements, each with its lineage roots."""
    entries = engine.canon(verdict="CANON", include_retired=False, limit=200)
    doctrines = [
        DoctrineModel(
            fragment=entry.fragment,
            score=entry.final_score,
            lineage_root_ids=_lineage_root_ids(engine.cfg, entry.cycle_id),
        )
        for entry in entries
    ]
    return DoctrinesResponse(doctrines=doctrines)


def build_contradictions(engine: FieldHorizonEngine, limit: int = CONTRADICTION_LIMIT) -> ContradictionsResponse:
    """Top ontological pressure edges: the two axiom statements in tension and why."""
    nodes = load_nodes(engine.cfg)
    nodes_by_id = {n.id: n for n in nodes}
    edges = sorted(build_pressure_edges(nodes), key=lambda e: e.score, reverse=True)[:limit]

    contradictions = []
    for edge in edges:
        source = nodes_by_id.get(edge.source_id)
        target = nodes_by_id.get(edge.target_id)
        if source is None or target is None:
            continue
        contradictions.append(
            ContradictionModel(
                source_id=edge.source_id,
                target_id=edge.target_id,
                pressure_type=edge.pressure_type,
                statement_a=source.statement,
                statement_b=target.statement,
                reason=edge.reason,
                pressure_score=edge.score,
            )
        )
    return ContradictionsResponse(contradictions=contradictions)


def build_beliefs(engine: FieldHorizonEngine) -> BeliefsResponse:
    """Per-school sample fragments suitable as NPC belief seeds."""
    beliefs = []
    for school in engine.schools():
        member_ids = school_member_cycle_ids(engine.cfg, school.id)[:BELIEF_SAMPLE_SIZE]
        if not member_ids:
            continue
        with connect(engine.cfg.database) as conn:
            placeholders = ",".join("?" for _ in member_ids)
            rows = conn.execute(f"SELECT fragment FROM cycles WHERE id IN ({placeholders})", member_ids).fetchall()
        fragments = [row["fragment"] for row in rows if row["fragment"]]
        if fragments:
            beliefs.append(BeliefModel(school_name=school.name, sample_fragments=fragments))
    return BeliefsResponse(beliefs=beliefs)


def export_godot(cfg: AppConfig, out_dir: Path) -> dict[str, Path]:
    """
    Writes the five versioned Godot export files. Read-only: consumes the
    facade and ontology/schools machinery, writes nothing back into the
    engine's own state. See docs/godot_schema.md for the field-by-field
    contract.
    """
    engine = FieldHorizonEngine(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)

    weather_snapshot = engine.weather()
    weather_payload = {
        "schema_version": SCHEMA_VERSION,
        "canon": weather_snapshot.canon,
        "surface": weather_snapshot.surface,
        "surface_history": weather_snapshot.surface_history,
    }

    payloads = {
        "factions.json": build_factions(engine).model_dump(),
        "doctrines.json": build_doctrines(engine).model_dump(),
        "contradictions.json": build_contradictions(engine).model_dump(),
        "weather.json": weather_payload,
        "beliefs.json": build_beliefs(engine).model_dump(),
    }

    written = {}
    for filename, payload in payloads.items():
        path = out_dir / filename
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written[filename] = path

    return written
