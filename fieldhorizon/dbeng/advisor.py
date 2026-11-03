"""
Deterministic index advisor and structural findings.

Candidates come from *measured* plans, not from guesses: a sequential scan
that read many rows to return few, filtered by AND-ed comparisons of plain
columns against constants, is a candidate for a b-tree on those columns
(equality columns first, then at most one range column). Every candidate
carries the plan evidence it came from. Whether it may be built is the
policy's decision; whether it is kept is the benchmark's.
"""
from __future__ import annotations

import hashlib

from .model import Candidate, ExplainResult, IndexSpec

MIN_SCANNED_ROWS = 10_000
MAX_SCAN_SELECTIVITY = 0.05
MAX_COLUMNS = 3

_EQUALITY = {"="}
_RANGE = {"<", "<=", ">", ">="}


def candidate_id(spec: IndexSpec) -> str:
    return "C-" + hashlib.sha256(spec.key.encode()).hexdigest()[:8]


def propose_index_candidates(plans: dict[str, ExplainResult], default_schema: str = "public") -> tuple[list[Candidate], list[dict]]:
    """
    Returns (candidates, observations). ``plans`` maps a workload query id
    to its baseline EXPLAIN ANALYZE. Observations record every selective
    sequential scan, including the ones no candidate could be derived from
    (OR predicates, functions on columns), so a skipped opportunity is
    visible instead of silently absent.
    """
    by_key: dict[str, Candidate] = {}
    observations: list[dict] = []
    for query_id, result in plans.items():
        for scan in result.seq_scans:
            selectivity = scan.get("selectivity")
            scanned = (scan.get("rows_returned") or 0) + (scan.get("rows_removed_by_filter") or 0)
            observation = {
                "query_id": query_id,
                "relation": f"{scan.get('schema') or default_schema}.{scan['relation']}",
                "filter": scan.get("filter"),
                "rows_scanned": scanned,
                "selectivity": selectivity,
            }
            if scanned < MIN_SCANNED_ROWS or selectivity is None or selectivity > MAX_SCAN_SELECTIVITY:
                observation["outcome"] = "not selective enough to index (scan reads what it returns)"
                observations.append(observation)
                continue
            predicates = scan.get("predicates") or []
            if not predicates:
                observation["outcome"] = "selective, but the filter is not a plain-column comparison an ordinary b-tree serves"
                observations.append(observation)
                continue
            equality = list(dict.fromkeys(p["column"] for p in predicates if p["op"] in _EQUALITY))
            ranged = [p["column"] for p in predicates if p["op"] in _RANGE and p["column"] not in equality]
            columns = (equality + ranged[:1])[:MAX_COLUMNS]
            spec = IndexSpec(schema=scan.get("schema") or default_schema, table=scan["relation"], columns=tuple(columns))
            observation["outcome"] = f"candidate {spec.key}"
            observations.append(observation)
            evidence = f"plan:{query_id}"
            if spec.key in by_key:
                existing = by_key[spec.key]
                existing.target_queries.append(query_id)
                existing.evidence_refs.append(evidence)
                continue
            by_key[spec.key] = Candidate(
                candidate_id=candidate_id(spec),
                spec=spec,
                source="advisor",
                rationale=(
                    f"Sequential scan of {spec.schema}.{spec.table} read {int(scanned)} rows to return "
                    f"{int(scan.get('rows_returned') or 0)} (selectivity {selectivity:.5f}) under filter {scan.get('filter')}."
                ),
                evidence_refs=[evidence],
                target_queries=[query_id],
                equality_columns=[c for c in columns if c in equality],
                range_columns=[c for c in columns if c not in equality],
            )
    return list(by_key.values()), observations


def manual_candidate(schema: str, table: str, columns: list[str], source: str = "operator") -> Candidate:
    spec = IndexSpec(schema=schema, table=table, columns=tuple(columns))
    return Candidate(
        candidate_id=candidate_id(spec), spec=spec, source=source,
        rationale="Submitted explicitly; subject to the same policy gate and benchmark as any advisor candidate.",
        equality_columns=list(columns),
    )


def parse_candidate_argument(text: str) -> Candidate:
    """'schema.table(col_a, col_b)' -> Candidate (operator-submitted)."""
    head, _, rest = text.strip().partition("(")
    if not rest.endswith(")") or "." not in head:
        raise ValueError(f"expected schema.table(col[, col...]), got {text!r}")
    schema, table = head.strip().split(".", 1)
    columns = [c.strip() for c in rest[:-1].split(",") if c.strip()]
    if not columns:
        raise ValueError(f"no columns in {text!r}")
    return manual_candidate(schema.strip(), table.strip(), columns)


def structural_findings(snapshot: dict) -> list[dict]:
    """Deterministic schema findings from a discovery snapshot, each with the facts that support it."""
    findings: list[dict] = []
    indexes_by_table: dict[tuple[str, str], list[dict]] = {}
    for idx in snapshot.get("indexes", []):
        indexes_by_table.setdefault((idx["schema"], idx["table"]), []).append(idx)
    constraints = snapshot.get("constraints", [])
    has_pk = {(c["schema"], c["table"]) for c in constraints if c["type"] == "p"}
    stats = {(s["schema"], s["table"]): s for s in snapshot.get("table_statistics", [])}

    for table in snapshot.get("tables", []):
        key = (table["schema"], table["table"])
        ref = f"table:{key[0]}.{key[1]}"
        if table["kind"] == "table" and key not in has_pk:
            findings.append({"kind": "missing_primary_key", "object": ref, "evidence": [ref]})
        if table.get("column_statistics") != "available" and (table.get("estimated_rows") or 0) > 0:
            findings.append({"kind": "missing_column_statistics", "object": ref, "evidence": [ref],
                             "detail": "pg_stats has no rows for this table (never analyzed, or no privilege)"})
        st = stats.get(key)
        if st and (st.get("n_live_tup") or 0) >= MIN_SCANNED_ROWS and (st.get("seq_scan") or 0) > 0 and not (st.get("idx_scan") or 0):
            findings.append({"kind": "sequentially_scanned_large_table", "object": ref, "evidence": [ref, f"stats:{key[0]}.{key[1]}"],
                             "detail": f"seq_scan={st.get('seq_scan')}, idx_scan={st.get('idx_scan')}, n_live_tup={st.get('n_live_tup')}"})

    for con in constraints:
        if con["type"] != "f":
            continue
        key = (con["schema"], con["table"])
        cols = list(con["columns"])
        covered = any(list(i["columns"][: len(cols)]) == cols and i["is_valid"] for i in indexes_by_table.get(key, []))
        if not covered:
            findings.append({
                "kind": "foreign_key_without_index", "object": f"constraint:{key[0]}.{key[1]}.{con['name']}",
                "evidence": [f"table:{key[0]}.{key[1]}"], "detail": f"columns {cols} reference {con['references']}; no index leads with them",
            })

    for key, idxs in indexes_by_table.items():
        for idx in idxs:
            if not idx["is_valid"]:
                findings.append({"kind": "invalid_index", "object": f"index:{key[0]}.{idx['index']}", "evidence": [f"table:{key[0]}.{key[1]}"]})
        signatures: dict[tuple, str] = {}
        for idx in idxs:
            if idx["has_expressions"] or idx["is_partial"]:
                continue
            sig = (idx["method"], tuple(idx["columns"]))
            if sig in signatures:
                findings.append({"kind": "duplicate_index", "object": f"index:{key[0]}.{idx['index']}",
                                 "evidence": [f"table:{key[0]}.{key[1]}"], "detail": f"same definition as {signatures[sig]}"})
            signatures[sig] = idx["index"]
    return findings
