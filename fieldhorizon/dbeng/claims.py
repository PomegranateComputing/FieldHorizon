"""
Agent claims, checked against the evidence they cite -- where a check is possible.

An accepted agent finding is a sentence plus evidence ids. Citation
existence was already enforced (agents.validate_response). Here, each finding
gets three separate answers, never merged into one "confidence":

* relevance  -- do the cited evidence items mention what the claim is about
                (its tables, columns, queries)? relevant / irrelevant / unknown;
* assertions -- the testable statements recognised in the claim, each
                verified or contradicted against the snapshot and the measured
                plans: a table or column exists, a query sequentially scans a
                table, a column has no index leading with it;
* verdict    -- contradicted if any assertion is contradicted; verified if at
                least one is verified and none contradicted; otherwise
                unverified (nothing in the sentence could be tested).

Recognition is deliberately literal (names that exist in the snapshot,
fixed phrases); what it does not recognise stays "unverified" rather than
being guessed. None of this feeds a decision: it is audit information.
"""
from __future__ import annotations

import json
import re

VERIFIED = "verified"
CONTRADICTED = "contradicted"
UNVERIFIED = "unverified"

_SEQ_SCAN = re.compile(r"\b(seq(uential)?\s*scan|full\s+(table\s+)?scan|scans?\s+the\s+(whole|entire)\s+table)", re.IGNORECASE)
_NO_INDEX = re.compile(r"\b(no\s+(supporting\s+)?index|missing\s+(an\s+)?index|without\s+(an?\s+|any\s+)?(supporting\s+)?index|"
                       r"unindexed|not\s+indexed|lacks?\s+(an?\s+)?index)", re.IGNORECASE)
_QUALIFIED = re.compile(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b")


def _facts(snapshot: dict, evidence: dict[str, dict]) -> dict:
    tables = {f"{t['schema']}.{t['table']}" for t in snapshot.get("tables", [])}
    columns: dict[str, set[str]] = {}
    for c in snapshot.get("columns", []):
        columns.setdefault(f"{c['schema']}.{c['table']}", set()).add(c["column"])
    leading: dict[str, set[str]] = {}
    for i in snapshot.get("indexes", []):
        if i.get("is_valid") and i.get("columns"):
            leading.setdefault(f"{i['schema']}.{i['table']}", set()).add(i["columns"][0])
    seq_scans: dict[str, set[str]] = {}
    for item in evidence.values():
        if item.get("kind") == "baseline_plan":
            for scan in item.get("seq_scans", []):
                seq_scans.setdefault(f"{scan.get('schema') or 'public'}.{scan['relation']}", set()).add(item["query_id"])
    return {"tables": tables, "bare": {t.split(".", 1)[1]: t for t in tables}, "columns": columns,
            "leading": leading, "seq_scans": seq_scans, "queries": {e.get("query_id") for e in evidence.values() if e.get("query_id")}}


def _subjects(text: str, facts: dict) -> dict:
    low = text.lower()
    tables, unknown = set(), set()
    schemas = {t.split(".", 1)[0] for t in facts["tables"]}
    for schema, name in _QUALIFIED.findall(low):
        qualified = f"{schema}.{name}"
        if qualified in facts["tables"]:
            tables.add(qualified)
        elif schema in schemas:
            unknown.add(qualified)  # a table, in a schema that exists, that does not
    for bare, qualified in facts["bare"].items():
        if re.search(rf"\b{re.escape(bare)}\b", low):
            tables.add(qualified)
    columns = set()
    for table in tables:
        for col in facts["columns"].get(table, ()):
            if re.search(rf"\b{re.escape(col)}\b", low):
                columns.add((table, col))
    queries = {q for q in facts["queries"] if q and re.search(rf"\b{re.escape(q.lower())}\b", low)}
    return {"tables": tables, "unknown_tables": unknown, "columns": columns, "queries": queries}


def check_claim(text: str, cited: list[str], evidence: dict[str, dict], facts: dict) -> dict:
    subjects = _subjects(text, facts)
    assertions: list[dict] = []
    for qualified in sorted(subjects["unknown_tables"]):
        assertions.append({"type": "table_exists", "subject": qualified, "result": CONTRADICTED,
                           "detail": "no such table in the discovery snapshot"})
    for table in sorted(subjects["tables"]):
        assertions.append({"type": "table_exists", "subject": table, "result": VERIFIED, "detail": "present in the snapshot"})
    if _SEQ_SCAN.search(text):
        for table in sorted(subjects["tables"]):
            scanned_by = sorted(facts["seq_scans"].get(table, set()))
            assertions.append({"type": "seq_scan_observed", "subject": table, "result": VERIFIED if scanned_by else CONTRADICTED,
                               "detail": f"sequentially scanned by {scanned_by}" if scanned_by else "no baseline plan scans it sequentially"})
    if _NO_INDEX.search(text):
        for table, col in sorted(subjects["columns"]):
            indexed = col in facts["leading"].get(table, set())
            assertions.append({"type": "column_unindexed", "subject": f"{table}.{col}", "result": CONTRADICTED if indexed else VERIFIED,
                               "detail": "a valid index leads with this column" if indexed else "no valid index leads with this column"})
    terms = [t.split(".", 1)[1] for t in subjects["tables"]] + [c for _, c in subjects["columns"]] + list(subjects["queries"])
    if not terms or not cited:
        relevance = "unknown"
    else:
        texts = [json.dumps(evidence.get(e, {}), default=str).lower() for e in cited]
        relevance = "relevant" if any(any(t in body for t in terms) for body in texts) else "irrelevant"
    results = {a["result"] for a in assertions}
    verdict = CONTRADICTED if CONTRADICTED in results else VERIFIED if VERIFIED in results else UNVERIFIED
    return {"claim": text, "evidence": cited, "relevance": relevance, "assertions": assertions, "verdict": verdict}


def verify_agent_reports(reports: dict[str, dict], evidence: dict[str, dict], snapshot: dict) -> dict:
    facts = _facts(snapshot or {}, evidence)
    out: dict[str, list[dict]] = {}
    for role, report in reports.items():
        checked = []
        for item in report.get("accepted", []):
            if isinstance(item, dict) and isinstance(item.get("claim"), str):
                checked.append(check_claim(item["claim"], [str(e) for e in item.get("evidence", [])], evidence, facts))
        for prop in report.get("proposals", []):
            table = f"{prop.get('schema')}.{prop.get('table')}"
            cols = [str(c) for c in prop.get("columns", [])]
            known = table in facts["tables"]
            missing = [c for c in cols if c not in facts["columns"].get(table, set())]
            checked.append({
                "claim": f"proposal: index on {table}({', '.join(cols)})", "evidence": [str(e) for e in prop.get("evidence", [])],
                "relevance": "n/a",
                "assertions": [{"type": "table_exists", "subject": table, "result": VERIFIED if known else CONTRADICTED, "detail": ""},
                               *({"type": "column_exists", "subject": f"{table}.{c}", "result": CONTRADICTED, "detail": "no such column"}
                                 for c in missing)],
                "verdict": CONTRADICTED if (not known or missing) else VERIFIED,
            })
        out[role] = checked
    flat = [c for items in out.values() for c in items]
    summary = {v: sum(1 for c in flat if c["verdict"] == v) for v in (VERIFIED, CONTRADICTED, UNVERIFIED)}
    summary["irrelevant_citations"] = sum(1 for c in flat if c["relevance"] == "irrelevant")
    summary["total"] = len(flat)
    return {"summary": summary, "by_role": out,
            "method": "literal recognition of snapshot names and fixed phrases; unrecognised statements stay unverified"}
