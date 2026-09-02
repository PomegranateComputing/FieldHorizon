from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from rich.tree import Tree

from .config import AppConfig
from .db import connect
from .lineage import LineageNotFoundError, extract_raw_material, load_entry, resolve_raw_material_location
from .manifests import find_manifest_for_cycle

logger = logging.getLogger(__name__)

# Object types provenance_edges ever references -- exactly the six kinds of
# EXISTING row Implementation Brief III's Phase C scopes edges to. Any
# other type string is a bug, not a future extension point: this is a
# relational edge store over rows this system already has, not a new
# mirror table per object kind (see the Phase C dossier).
OBJECT_CYCLE = "cycle"
OBJECT_CHUNK = "chunk"
OBJECT_JSON_ENTRY = "json_entry"
OBJECT_SCHOOL = "school"
OBJECT_SOURCE = "source"
OBJECT_COUNCIL = "council"

# Relation types actually populated by this system (see the Phase C
# dossier's writer-by-writer table for exactly which operation writes
# each). CONTRADICTS, CLUSTERED_WITH, and GENERATED_BY are deliberately
# absent -- nothing in this repository emits them yet, and Implementation
# Brief III rule 2 forbids adding a relation type nothing writes.
REL_SELECTED_BY = "SELECTED_BY"
REL_MEMBER_OF = "MEMBER_OF"
REL_SUPERSEDES = "SUPERSEDES"
REL_REHABILITATES = "REHABILITATES"
REL_RETIRES = "RETIRES"
REL_PROMOTED_BY = "PROMOTED_BY"
REL_MUTATES = "MUTATES"
REL_DERIVED_FROM = "DERIVED_FROM"
REL_EXTRACTED_FROM = "EXTRACTED_FROM"
REL_SUPPORTS = "SUPPORTS"
REL_OPPOSES = "OPPOSES"

CREATION_WRITE_TIME = "write_time"
CREATION_BACKFILL = "backfill"


def object_ref(object_type: str, object_id: str | int) -> str:
    """The "{type}:{id}" string convention run_manifests.output_ids already uses for schools, applied uniformly to every object type an edge can reference."""
    return f"{object_type}:{object_id}"


@dataclass(frozen=True)
class ProvenanceEdge:
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    relation_type: str
    confidence: float = 1.0
    polarity: float | None = None
    evidence_ref: str | None = None
    provenance_ref: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    creation_method: str = CREATION_WRITE_TIME
    review_status: str = "unreviewed"
    id: int | None = None
    recorded_at: str | None = None


def _row_to_edge(row) -> ProvenanceEdge:
    return ProvenanceEdge(
        id=int(row["id"]),
        source_type=row["source_type"],
        source_id=row["source_id"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        relation_type=row["relation_type"],
        confidence=float(row["confidence"]),
        polarity=row["polarity"],
        evidence_ref=row["evidence_ref"],
        provenance_ref=row["provenance_ref"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        recorded_at=row["recorded_at"],
        creation_method=row["creation_method"],
        review_status=row["review_status"],
    )


class ProvenanceEdgeRepository:
    """
    Append-only store for provenance_edges, mirroring EventRepository's and
    RunManifestRepository's shape (fieldhorizon.events, fieldhorizon.manifests).
    `append` is idempotent by the table's natural-key unique index
    (source_type, source_id, target_type, target_id, relation_type) -- the
    same edge recorded twice (a live write followed by a later backfill
    pass over the same history) is INSERT OR IGNORE'd, never duplicated,
    and the first writer wins: a write-time edge is never clobbered by a
    later backfill attempting to reconstruct the same fact.
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def append(
        self,
        source_type: str,
        source_id: str | int,
        target_type: str,
        target_id: str | int,
        relation_type: str,
        confidence: float = 1.0,
        polarity: float | None = None,
        evidence_ref: str | None = None,
        provenance_ref: str | None = None,
        valid_from: str | None = None,
        creation_method: str = CREATION_WRITE_TIME,
    ) -> ProvenanceEdge:
        with connect(self.cfg.database) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO provenance_edges(
                    source_type, source_id, target_type, target_id, relation_type,
                    confidence, polarity, evidence_ref, provenance_ref, valid_from, creation_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_type, str(source_id), target_type, str(target_id), relation_type,
                    confidence, polarity, evidence_ref, provenance_ref, valid_from, creation_method,
                ),
            )
            conn.commit()

            # INSERT OR IGNORE means cursor.lastrowid is unreliable when the
            # natural key already existed (a backfill re-recording a
            # write-time fact) -- look the row up by natural key instead, so
            # callers needing the id (e.g. Phase D's source_edge_id linkage)
            # always get the TRUE row, whichever call actually wrote it.
            row = conn.execute(
                """
                SELECT id FROM provenance_edges
                WHERE source_type = ? AND source_id = ? AND target_type = ? AND target_id = ? AND relation_type = ?
                """,
                (source_type, str(source_id), target_type, str(target_id), relation_type),
            ).fetchone()

        return ProvenanceEdge(
            id=int(row["id"]) if row is not None else None,
            source_type=source_type,
            source_id=str(source_id),
            target_type=target_type,
            target_id=str(target_id),
            relation_type=relation_type,
            confidence=confidence,
            polarity=polarity,
            evidence_ref=evidence_ref,
            provenance_ref=provenance_ref,
            valid_from=valid_from,
            creation_method=creation_method,
        )

    def edges_from(
        self, source_type: str, source_id: str | int, relation_type: str | None = None
    ) -> list[ProvenanceEdge]:
        query = "SELECT * FROM provenance_edges WHERE source_type = ? AND source_id = ?"
        params: list = [source_type, str(source_id)]
        if relation_type:
            query += " AND relation_type = ?"
            params.append(relation_type)
        query += " ORDER BY id ASC"

        with connect(self.cfg.database) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_edge(row) for row in rows]

    def edges_to(
        self, target_type: str, target_id: str | int, relation_type: str | None = None
    ) -> list[ProvenanceEdge]:
        query = "SELECT * FROM provenance_edges WHERE target_type = ? AND target_id = ?"
        params: list = [target_type, str(target_id)]
        if relation_type:
            query += " AND relation_type = ?"
            params.append(relation_type)
        query += " ORDER BY id ASC"

        with connect(self.cfg.database) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_edge(row) for row in rows]

    def count(self) -> int:
        with connect(self.cfg.database) as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM provenance_edges").fetchone()["n"])

    def get_by_id(self, edge_id: int) -> ProvenanceEdge | None:
        with connect(self.cfg.database) as conn:
            row = conn.execute("SELECT * FROM provenance_edges WHERE id = ?", (edge_id,)).fetchone()
        return _row_to_edge(row) if row is not None else None


def resolve_chunk_id(cfg: AppConfig, canonical_ref: str) -> int | None:
    """
    cycle_sources.ref for book evidence is chunks.canonical_ref (a
    human-readable string retrieval.py returns -- see search_books/
    search_one_source), not chunks.id: chunks_fts carries no foreign key
    back to chunks (see ingest.ingest_books's own module docstring), and
    canonical_ref has no UNIQUE constraint in the schema. Resolves to an
    exact chunks.id only when precisely one match exists; returns None
    (never guesses) when zero or more than one chunk shares that ref, so
    an ambiguous SUPPORTS edge is skipped and logged by the caller rather
    than pointed at the wrong row.
    """
    with connect(cfg.database) as conn:
        rows = conn.execute("SELECT id FROM chunks WHERE canonical_ref = ?", (canonical_ref,)).fetchall()
    if len(rows) != 1:
        return None
    return int(rows[0]["id"])


def record_selected_by_edges(
    cfg: AppConfig,
    child_cycle_id: int,
    parent_cycle_ids: list[int],
    provenance_ref: str | None = None,
    creation_method: str = CREATION_WRITE_TIME,
) -> None:
    """
    One SELECTED_BY edge per parent cycle id already recorded on
    cycles.parent_cycle_ids (canon._select_by_mmr's result, at the exact
    moment cycle.py/multicycle.py write that column) -- the same fact,
    made traversable instead of only readable as a JSON blob on the child
    row.
    """
    repo = ProvenanceEdgeRepository(cfg)
    for parent_id in parent_cycle_ids:
        repo.append(
            OBJECT_CYCLE, parent_id, OBJECT_CYCLE, child_cycle_id, REL_SELECTED_BY,
            provenance_ref=provenance_ref, creation_method=creation_method,
        )


def record_supports_edges(
    cfg: AppConfig,
    cycle_id: int,
    evidence_refs: list[tuple[str, str | int]],
    provenance_ref: str | None = None,
    creation_method: str = CREATION_WRITE_TIME,
) -> int:
    """
    One SUPPORTS edge per non-canon evidence item cited in a cycle's
    prompt: `evidence_refs` is a list of (source_kind, ref) pairs drawn
    from exactly the same book_rows/json_rows already written to
    cycle_sources -- 'book' refs a chunks.canonical_ref (resolved via
    resolve_chunk_id; skipped and logged when ambiguous, never guessed),
    'json' refs a json_entries.id directly. 'canon' evidence is
    deliberately excluded here: a canon cycle_sources row and a
    parent_cycle_ids entry are the same underlying fact (both come from
    canon.load_canon_fragments), already covered by
    record_selected_by_edges -- writing a second SUPPORTS edge for it
    would duplicate that relationship under a different name. Returns the
    count of 'book' refs that could not be resolved to an exact chunk, so
    a backfill run can report exactly how many links it honestly could
    not reconstruct.
    """
    repo = ProvenanceEdgeRepository(cfg)
    unresolved = 0
    for source_kind, ref in evidence_refs:
        if source_kind == "book":
            chunk_id = resolve_chunk_id(cfg, str(ref))
            if chunk_id is None:
                logger.warning(
                    "Provenance: could not resolve book ref %r to an exact chunk for cycle %d", ref, cycle_id
                )
                unresolved += 1
                continue
            repo.append(
                OBJECT_CHUNK, chunk_id, OBJECT_CYCLE, cycle_id, REL_SUPPORTS,
                polarity=1.0, evidence_ref=str(ref), provenance_ref=provenance_ref, creation_method=creation_method,
            )
        elif source_kind == "json":
            repo.append(
                OBJECT_JSON_ENTRY, ref, OBJECT_CYCLE, cycle_id, REL_SUPPORTS,
                polarity=1.0, evidence_ref=str(ref), provenance_ref=provenance_ref, creation_method=creation_method,
            )
    return unresolved


def record_member_of_edges(
    cfg: AppConfig,
    cycle_ids: list[int],
    school_id: int,
    provenance_ref: str | None = None,
    creation_method: str = CREATION_WRITE_TIME,
) -> None:
    """One MEMBER_OF edge per cycle clustered into this school (schools.run_schools's school_members insert)."""
    repo = ProvenanceEdgeRepository(cfg)
    for cycle_id in cycle_ids:
        repo.append(
            OBJECT_CYCLE, cycle_id, OBJECT_SCHOOL, school_id, REL_MEMBER_OF,
            provenance_ref=provenance_ref, creation_method=creation_method,
        )


def record_supersedes_edge(
    cfg: AppConfig,
    school_id: int,
    previous_school_id: int | None,
    provenance_ref: str | None = None,
    creation_method: str = CREATION_WRITE_TIME,
) -> None:
    """
    A new school SUPERSEDES whichever prior school its membership
    overlaps with most (schools._best_overlapping_school's
    previous_school_id) -- no-op when a school has no overlapping
    predecessor.
    """
    if previous_school_id is None:
        return
    ProvenanceEdgeRepository(cfg).append(
        OBJECT_SCHOOL, school_id, OBJECT_SCHOOL, previous_school_id, REL_SUPERSEDES,
        provenance_ref=provenance_ref, creation_method=creation_method,
    )


def record_generated_axiom_edges(
    cfg: AppConfig,
    axiom_id: str,
    provenance: dict | None,
    creation_method: str = CREATION_WRITE_TIME,
) -> None:
    """
    Persists the MUTATES/DERIVED_FROM/OPPOSES facts already carried in a
    generated axiom's raw_json.provenance dict (ontology.export_generated_axioms's
    shape: {"source_edges": [...], "stratum": N, "source_cycle_ids": [...]}),
    at the moment ingest.ingest_json_corpus actually lands the row --
    persisting a fact that already existed as an opaque JSON blob read only
    by lineage.py, not inventing a new one. No-op when `provenance` is
    absent (a hand-authored axiom, never LLM-generated). MUTATES runs
    axiom -> each of source_id/target_id (the two nodes tension between
    which produced this axiom); DERIVED_FROM runs axiom -> each cited
    source_cycle_id (the canon lineage a SOURCE axiom's own gloss traces
    to, per ontology._source_cycle_ids_for_axiom); OPPOSES is the
    underlying axiom-vs-axiom ontological pressure itself (source_id vs
    target_id), persisted here -- and only here, not on every ad hoc
    `axiom-candidates`/`pressure-report` inspection run -- because this is
    the one moment that pressure edge produced a durable outcome.
    """
    if not provenance:
        return

    repo = ProvenanceEdgeRepository(cfg)
    for source_edge in provenance.get("source_edges") or []:
        source_id = source_edge.get("source_id")
        target_id = source_edge.get("target_id")

        if source_id:
            repo.append(OBJECT_JSON_ENTRY, axiom_id, OBJECT_JSON_ENTRY, source_id, REL_MUTATES, creation_method=creation_method)
        if target_id:
            repo.append(OBJECT_JSON_ENTRY, axiom_id, OBJECT_JSON_ENTRY, target_id, REL_MUTATES, creation_method=creation_method)

        if source_id and target_id:
            score = source_edge.get("pressure_score")
            confidence = min(1.0, max(0.0, float(score))) if isinstance(score, (int, float)) else 1.0
            repo.append(
                OBJECT_JSON_ENTRY, source_id, OBJECT_JSON_ENTRY, target_id, REL_OPPOSES,
                confidence=confidence, polarity=-1.0, evidence_ref=source_edge.get("reason"),
                creation_method=creation_method,
            )

    for cycle_id in provenance.get("source_cycle_ids") or []:
        repo.append(
            OBJECT_JSON_ENTRY, axiom_id, OBJECT_CYCLE, cycle_id, REL_DERIVED_FROM, creation_method=creation_method
        )


def record_extracted_from_edge(
    cfg: AppConfig,
    axiom_id: str,
    gloss: str,
    creation_method: str = CREATION_WRITE_TIME,
) -> None:
    """
    If this axiom's gloss cites raw distilled material
    (lineage.RAW_GLOSS_PREFIX) and that material resolves to an exact
    ingested chunk (lineage.resolve_raw_material_location), persists it as
    an EXTRACTED_FROM edge instead of recomputing the same O(n) text
    search on every lineage view. No-op when the gloss carries no such
    citation, or the raw material can't be located in any ingested chunk
    (both common and expected -- see resolve_raw_material_location's own
    docstring).
    """
    raw_material = extract_raw_material(gloss or "")
    if not raw_material:
        return

    location = resolve_raw_material_location(cfg, raw_material)
    if location is None:
        return

    ProvenanceEdgeRepository(cfg).append(
        OBJECT_JSON_ENTRY, axiom_id, OBJECT_CHUNK, location["chunk_id"], REL_EXTRACTED_FROM,
        evidence_ref=f"{location['char_start']}:{location['char_end']}", creation_method=creation_method,
    )


def _council_id_for_canon_event(cfg: AppConfig, canon_event_id: int) -> int | None:
    """
    canon_events carries no council_id column of its own -- the only way
    to attribute a REHABILITATED/RETIRED/council-triggered PROMOTED row to
    the specific council that caused it is via Phase A's domain_events:
    genealogy.record_canon_event cross-emits into domain_events with
    canon_event_id pointing back at this exact row, sharing the SAME
    correlation_id as that operation's other events -- including the
    CouncilStarted/CouncilCompleted event that carries aggregate_id=council_id.
    Returns None (never guesses) when no such domain_events row exists,
    which is expected for any canon_events row older than Phase A itself.
    """
    with connect(cfg.database) as conn:
        de_row = conn.execute(
            "SELECT correlation_id FROM domain_events WHERE canon_event_id = ?", (canon_event_id,)
        ).fetchone()
        if de_row is None:
            return None

        council_row = conn.execute(
            "SELECT aggregate_id FROM domain_events WHERE correlation_id = ? AND aggregate_type = 'council' LIMIT 1",
            (de_row["correlation_id"],),
        ).fetchone()

    if council_row is None or council_row["aggregate_id"] is None:
        return None
    return int(council_row["aggregate_id"])


@dataclass(frozen=True)
class BackfillReport:
    edges_before: int
    edges_after: int
    unresolved_book_refs: int
    unattributed_canon_events: int

    @property
    def edges_added(self) -> int:
        return self.edges_after - self.edges_before


def backfill_provenance_edges(cfg: AppConfig) -> BackfillReport:
    """
    One-shot historical reconstruction over an existing corpus -- safe to
    re-run any time: idempotent by provenance_edges' own natural key (see
    ProvenanceEdgeRepository.append), so already-recorded facts are never
    duplicated and a write-time edge is never clobbered by a backfill
    attempt at the same fact.

    Two categories of gap are reported, not silently absorbed: a 'book'
    cycle_sources ref that can't be resolved to an exact chunk
    (unresolved_book_refs, see resolve_chunk_id), and a REHABILITATED/
    RETIRED/council-triggered-PROMOTED canon_events row with no
    corresponding domain_events row to recover its council_id from
    (unattributed_canon_events, see _council_id_for_canon_event) --
    expected for any history older than Phase A's domain_events table.
    Both counts are legitimate historical gaps this backfill cannot close
    without guessing, not bugs in the backfill itself.
    """
    repo = ProvenanceEdgeRepository(cfg)
    edges_before = repo.count()
    unresolved_book_refs = 0
    unattributed_canon_events = 0

    with connect(cfg.database) as conn:
        cycle_rows = conn.execute("SELECT id, parent_cycle_ids FROM cycles").fetchall()
        cycle_source_rows = conn.execute("SELECT cycle_id, source_kind, ref FROM cycle_sources").fetchall()
        school_member_rows = conn.execute("SELECT school_id, cycle_id FROM school_members").fetchall()
        school_rows = conn.execute("SELECT id, previous_school_id FROM schools").fetchall()
        canon_event_rows = conn.execute("SELECT id, cycle_id, event FROM canon_events").fetchall()
        json_entry_rows = conn.execute("SELECT id, raw_json, gloss FROM json_entries").fetchall()

    for row in cycle_rows:
        if not row["parent_cycle_ids"]:
            continue
        try:
            parent_ids = json.loads(row["parent_cycle_ids"]) or []
        except (json.JSONDecodeError, TypeError):
            parent_ids = []
        if parent_ids:
            record_selected_by_edges(
                cfg, int(row["id"]), [int(p) for p in parent_ids], creation_method=CREATION_BACKFILL
            )

    sources_by_cycle: dict[int, list[tuple[str, str | int]]] = {}
    for row in cycle_source_rows:
        if row["source_kind"] in ("book", "json"):
            sources_by_cycle.setdefault(int(row["cycle_id"]), []).append((row["source_kind"], row["ref"]))
    for cycle_id, refs in sources_by_cycle.items():
        unresolved_book_refs += record_supports_edges(cfg, cycle_id, refs, creation_method=CREATION_BACKFILL)

    members_by_school: dict[int, list[int]] = {}
    for row in school_member_rows:
        members_by_school.setdefault(int(row["school_id"]), []).append(int(row["cycle_id"]))
    for school_id, cycle_ids in members_by_school.items():
        record_member_of_edges(cfg, cycle_ids, school_id, creation_method=CREATION_BACKFILL)

    for row in school_rows:
        if row["previous_school_id"] is not None:
            record_supersedes_edge(
                cfg, int(row["id"]), int(row["previous_school_id"]), creation_method=CREATION_BACKFILL
            )

    for row in canon_event_rows:
        if row["event"] not in ("REHABILITATED", "RETIRED", "PROMOTED"):
            continue
        council_id = _council_id_for_canon_event(cfg, int(row["id"]))
        if council_id is None:
            if row["event"] != "PROMOTED":
                # A council-caused PROMOTED that can't be attributed is
                # indistinguishable from an original write-time promotion
                # (which correctly has no council to attribute) -- only
                # count the unambiguous REHABILITATED/RETIRED gap.
                unattributed_canon_events += 1
            continue
        if row["event"] == "REHABILITATED":
            repo.append(OBJECT_COUNCIL, council_id, OBJECT_CYCLE, int(row["cycle_id"]), REL_REHABILITATES, creation_method=CREATION_BACKFILL)
        elif row["event"] == "RETIRED":
            repo.append(OBJECT_COUNCIL, council_id, OBJECT_CYCLE, int(row["cycle_id"]), REL_RETIRES, creation_method=CREATION_BACKFILL)
        else:  # PROMOTED, attributable to a council -- the rehabilitation-to-CANON case
            repo.append(OBJECT_CYCLE, int(row["cycle_id"]), OBJECT_COUNCIL, council_id, REL_PROMOTED_BY, creation_method=CREATION_BACKFILL)

    for row in json_entry_rows:
        try:
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        except (json.JSONDecodeError, TypeError):
            raw = {}
        provenance = raw.get("provenance") if isinstance(raw, dict) else None
        record_generated_axiom_edges(cfg, row["id"], provenance, creation_method=CREATION_BACKFILL)
        record_extracted_from_edge(cfg, row["id"], row["gloss"] or "", creation_method=CREATION_BACKFILL)

    return BackfillReport(
        edges_before=edges_before,
        edges_after=repo.count(),
        unresolved_book_refs=unresolved_book_refs,
        unattributed_canon_events=unattributed_canon_events,
    )


# Epistemic supply chain (Phase C dossier §5c). "Ancestry" is deliberately
# narrower than every edge touching an object: for a cycle it's the
# SELECTED_BY/SUPPORTS edges pointing AT it (its parents and its cited
# evidence); for a json_entry axiom it's the DERIVED_FROM/MUTATES/
# EXTRACTED_FROM edges pointing FROM it (what it was generated out of).
# MEMBER_OF/SUPERSEDES/REHABILITATES/RETIRES/PROMOTED_BY/OPPOSES describe
# something ELSE about an object (its clustering, its judgment history, an
# axiom's ontological rival) -- not what it was built from -- so they are
# not part of an ancestry walk.
_CYCLE_ANCESTRY_RELATIONS = (REL_SELECTED_BY, REL_SUPPORTS)
_AXIOM_ANCESTRY_RELATIONS = (REL_DERIVED_FROM, REL_MUTATES, REL_EXTRACTED_FROM)


def _ancestors_of(cfg: AppConfig, object_type: str, object_id: str) -> list[tuple[str, str, ProvenanceEdge]]:
    repo = ProvenanceEdgeRepository(cfg)
    results: list[tuple[str, str, ProvenanceEdge]] = []

    if object_type == OBJECT_CYCLE:
        for relation in _CYCLE_ANCESTRY_RELATIONS:
            for edge in repo.edges_to(object_type, object_id, relation_type=relation):
                results.append((edge.source_type, edge.source_id, edge))
    elif object_type == OBJECT_JSON_ENTRY:
        for relation in _AXIOM_ANCESTRY_RELATIONS:
            for edge in repo.edges_from(object_type, object_id, relation_type=relation):
                results.append((edge.target_type, edge.target_id, edge))

    return results


def _walk_ancestry(
    cfg: AppConfig, object_type: str, object_id: str, visited: frozenset[tuple[str, str]] = frozenset()
) -> tuple[bool, list[tuple[str, str]], list[ProvenanceEdge]]:
    """
    Returns (circular, leaves, edges). `visited` is passed BY VALUE down
    each recursive call (never mutated in place) -- the same discipline
    lineage.py's own cycle-detection already uses -- so two branches
    legitimately sharing one ancestor (an ordinary DAG diamond: e.g. two
    cycles both citing the same axiom) is never mistaken for a cycle; only
    a node repeating along the SAME path is.
    """
    key = (object_type, str(object_id))
    if key in visited:
        return True, [], []

    ancestors = _ancestors_of(cfg, object_type, str(object_id))
    if not ancestors:
        return False, [key], []

    circular = False
    leaves: list[tuple[str, str]] = []
    edges: list[ProvenanceEdge] = []
    next_visited = visited | {key}

    for parent_type, parent_id, edge in ancestors:
        edges.append(edge)
        sub_circular, sub_leaves, sub_edges = _walk_ancestry(cfg, parent_type, parent_id, next_visited)
        circular = circular or sub_circular
        leaves.extend(sub_leaves)
        edges.extend(sub_edges)

    return circular, leaves, edges


def _is_sourced_leaf(cfg: AppConfig, leaf_type: str, leaf_id: str) -> bool:
    """
    A leaf counts as grounded in the real corpus if it's a chunk (an
    ingested source fragment) or a json_entry axiom with its own
    EXTRACTED_FROM edge to one. Everything else -- a hand-authored axiom
    with no raw-material citation, or a cycle with no recorded ancestry at
    all -- is "synthetic": asserted or generated within this system, not
    traceable to external text. Deliberately NOT keyed off json_entries.tradition
    (see the Phase C dossier: that field's values are inconsistent across
    writers and not a reliable governance signal).
    """
    if leaf_type == OBJECT_CHUNK:
        return True
    if leaf_type == OBJECT_JSON_ENTRY:
        return bool(ProvenanceEdgeRepository(cfg).edges_from(leaf_type, leaf_id, relation_type=REL_EXTRACTED_FROM))
    return False


def _synthetic_dependency_ratio(cfg: AppConfig, leaves: list[tuple[str, str]]) -> float:
    if not leaves:
        return 0.0
    sourced = sum(1 for leaf_type, leaf_id in leaves if _is_sourced_leaf(cfg, leaf_type, leaf_id))
    return 1.0 - (sourced / len(leaves))


def _weakest_link(edges: list[ProvenanceEdge]) -> ProvenanceEdge | None:
    if not edges:
        return None
    return min(edges, key=lambda e: e.confidence)


def _cycle_completeness_score(cfg: AppConfig, cycle_id: int) -> tuple[float, list[str]]:
    """
    expected = the SELECTED_BY/SUPPORTS facts a cycle's OWN rows already
    promise (parent_cycle_ids entries + book/json cycle_sources rows);
    actual = how many of those are actually present in provenance_edges.
    Missing links are named explicitly, not folded into a bare fraction --
    an honest completeness score, not a vibe (Phase C dossier §5c).
    """
    with connect(cfg.database) as conn:
        cycle_row = conn.execute("SELECT parent_cycle_ids FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        source_rows = conn.execute(
            "SELECT source_kind, ref FROM cycle_sources WHERE cycle_id = ? AND source_kind IN ('book', 'json')",
            (cycle_id,),
        ).fetchall()

    parent_ids: list[int] = []
    if cycle_row is not None and cycle_row["parent_cycle_ids"]:
        try:
            parent_ids = json.loads(cycle_row["parent_cycle_ids"]) or []
        except (json.JSONDecodeError, TypeError):
            parent_ids = []

    expected = len(parent_ids) + len(source_rows)
    if expected == 0:
        return 1.0, []  # nothing was expected to produce an edge -- vacuously complete, not "0 of 0 missing"

    repo = ProvenanceEdgeRepository(cfg)
    actual_parents = {e.source_id for e in repo.edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_SELECTED_BY)}
    actual_evidence = {
        (e.source_type, e.source_id) for e in repo.edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_SUPPORTS)
    }

    found = 0
    missing: list[str] = []

    for parent_id in parent_ids:
        if str(parent_id) in actual_parents:
            found += 1
        else:
            missing.append(f"missing SELECTED_BY edge for parent cycle {parent_id}")

    for row in source_rows:
        if row["source_kind"] == "json":
            if (OBJECT_JSON_ENTRY, str(row["ref"])) in actual_evidence:
                found += 1
            else:
                missing.append(f"missing SUPPORTS edge for json ref {row['ref']!r}")
        else:  # 'book'
            chunk_id = resolve_chunk_id(cfg, row["ref"])
            if chunk_id is not None and (OBJECT_CHUNK, str(chunk_id)) in actual_evidence:
                found += 1
            else:
                missing.append(f"missing SUPPORTS edge for book ref {row['ref']!r}")

    return found / expected, missing


@dataclass(frozen=True)
class ProvenanceReport:
    object_type: str
    object_id: str
    supporting_evidence_count: int
    opposing_evidence_count: int
    circular_ancestry: bool
    synthetic_dependency_ratio: float
    weakest_link: ProvenanceEdge | None
    completeness_score: float
    missing_links: list[str]
    leaves: list[tuple[str, str]]
    model_registry_ids: list[str]


def build_provenance_report(cfg: AppConfig, object_type: str, object_id: str | int) -> ProvenanceReport:
    """
    The epistemic supply chain for one object: circular-ancestry detection,
    synthetic-dependency ratio, the weakest (lowest-confidence) provenance
    link, and -- for cycles only, where the underlying facts exist --
    supporting/opposing evidence counts, a provenance-completeness score,
    and which Phase B model ids drove its one LLM-generated step.
    opposing_evidence_count is always 0 today: no writer in this codebase
    ever produces a CONTRADICTS or evidence-level OPPOSES edge (see the
    Phase C dossier) -- reported honestly, not fabricated.
    """
    circular, leaves, edges = _walk_ancestry(cfg, object_type, str(object_id))

    supporting_evidence_count = 0
    completeness_score = 1.0
    missing_links: list[str] = []
    model_registry_ids: list[str] = []

    if object_type == OBJECT_CYCLE:
        cycle_id = int(object_id)
        supporting_evidence_count = len(
            ProvenanceEdgeRepository(cfg).edges_to(OBJECT_CYCLE, cycle_id, relation_type=REL_SUPPORTS)
        )
        completeness_score, missing_links = _cycle_completeness_score(cfg, cycle_id)

        manifest = find_manifest_for_cycle(cfg, cycle_id)
        if manifest is not None and manifest.model_registry_ids:
            model_registry_ids = list(manifest.model_registry_ids)

    return ProvenanceReport(
        object_type=object_type,
        object_id=str(object_id),
        supporting_evidence_count=supporting_evidence_count,
        opposing_evidence_count=0,
        circular_ancestry=circular,
        synthetic_dependency_ratio=_synthetic_dependency_ratio(cfg, leaves),
        weakest_link=_weakest_link(edges),
        completeness_score=completeness_score,
        missing_links=missing_links,
        leaves=leaves,
        model_registry_ids=model_registry_ids,
    )


def build_provenance_report_for_target(cfg: AppConfig, target_id: str) -> ProvenanceReport:
    """
    Dual dispatch matching lineage.build_lineage_tree exactly: a numeric
    target_id is a cycle, anything else a json_entries axiom id. Existence
    is checked the same way build_lineage_tree's own branches do (a missing
    row raises LineageNotFoundError) -- without this, a nonexistent id would
    silently produce a "vacuously complete" report (see
    _cycle_completeness_score's expected==0 case) instead of a 404.
    """
    stripped = target_id.strip()
    if stripped.lstrip("-").isdigit():
        cycle_id = int(stripped)
        with connect(cfg.database) as conn:
            row = conn.execute("SELECT 1 FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
        if row is None:
            raise LineageNotFoundError(f"No cycles row found for id={cycle_id!r}")
        return build_provenance_report(cfg, OBJECT_CYCLE, cycle_id)

    if load_entry(cfg, stripped) is None:
        raise LineageNotFoundError(f"No json_entries row found for id={stripped!r}")
    return build_provenance_report(cfg, OBJECT_JSON_ENTRY, stripped)


def add_provenance_graph_section(tree: Tree, cfg: AppConfig, object_type: str, object_id: str | int) -> None:
    """
    Appends the full provenance graph -- every edge touching this object,
    in both directions -- to an existing lineage.py Tree (Phase C's
    "extend field-horizon lineage to render the full graph up and down").
    Shown ALONGSIDE, not instead of, the object-specific genealogy/axiom-
    provenance tree lineage.py already builds: this section is the
    complete edge list, including relation types (MEMBER_OF,
    REHABILITATES, SUPERSEDES, ...) that tree never covers. No-op when
    the object has no recorded edges at all.
    """
    repo = ProvenanceEdgeRepository(cfg)
    outgoing = repo.edges_from(object_type, object_id)
    incoming = repo.edges_to(object_type, object_id)
    if not outgoing and not incoming:
        return

    graph_node = tree.add(f"[bold]Provenance graph[/bold] ({object_type}:{object_id})")
    if outgoing:
        out_node = graph_node.add("Outgoing")
        for edge in outgoing:
            out_node.add(f"[yellow]{edge.relation_type}[/yellow] -> {edge.target_type}:{edge.target_id}")
    if incoming:
        in_node = graph_node.add("Incoming")
        for edge in incoming:
            in_node.add(f"[yellow]{edge.relation_type}[/yellow] <- {edge.source_type}:{edge.source_id}")
