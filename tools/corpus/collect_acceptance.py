"""
Collect the acceptance evidence for the corpus harvester pilot.

Reads only; writes reports/CORPUS_HARVESTER_ACCEPTANCE.json. Everything
it reports is queried from the database and the filesystem rather than
supplied by hand, so the acceptance report cannot drift from what
actually happened.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fieldhorizon.config import load_config
from fieldhorizon.corpus.ingestion import MANIFEST_PREFIX, indexed_document_ids
from fieldhorizon.corpus.models import State
from fieldhorizon.corpus.repository import CorpusRepository
from fieldhorizon.corpus.storage import CorpusStorage, human_bytes
from fieldhorizon.db import connect

cfg = load_config(str(Path(__file__).resolve().parents[2] / "config.yaml"))
repo = CorpusRepository(cfg.database)
storage = CorpusStorage(cfg.root / "data" / "corpus")

out: dict = {}

# ---------------------------------------------------------------- runs
runs = []
with connect(cfg.database) as c:
    for r in c.execute("SELECT * FROM corpus_runs ORDER BY started_at"):
        row = dict(r)
        row["stats"] = json.loads(row.pop("stats_json") or "{}")
        runs.append(row)
out["runs"] = runs

# ------------------------------------------------------------- sources
out["source_runs"] = []
with connect(cfg.database) as c:
    for r in c.execute("SELECT * FROM corpus_source_runs ORDER BY id"):
        out["source_runs"].append(dict(r))

# --------------------------------------------------------------- items
items = repo.all_items()
out["item_states"] = repo.count_items_by_state()
out["candidate_states"] = repo.count_candidates_by_state()

indexed = [i for i in items if i.state in (State.INGESTED, State.INDEXED)]
out["indexed_count"] = len(indexed)
out["indexed_documents"] = [
    {
        "document_id": i.document_id,
        "title": i.title,
        "authors": i.authors,
        "language": i.language,
        "destination": i.destination.value if i.destination else None,
        "source": i.source_id,
        "license": i.normalized_license,
        "distribution_scope": i.distribution_scope,
        "quality_score": i.quality_score,
        "canonical_url": i.canonical_url,
        "raw_sha256": i.raw_sha256,
        "normalized_sha256": i.normalized_sha256,
        "materialized_path": i.materialized_path,
        "attribution_required": i.attribution_required,
        "share_alike": i.share_alike,
    }
    for i in indexed
]

out["distributions"] = {
    k: repo.distribution(k)
    for k in ("language", "destination", "normalized_license", "source_id",
              "distribution_scope", "state")
}

# ------------------------------------------------ the critical invariant
# No document may be indexed without an accept decision backed by evidence.
violations = []
for i in indexed:
    d = repo.latest_rights_decision(i.document_id)
    if not d or d.get("decision") != "accept":
        violations.append({"document_id": i.document_id, "reason": "no accept decision"})
        continue
    if not json.loads(d.get("evidence_json") or "[]"):
        violations.append({"document_id": i.document_id, "reason": "accept with no evidence"})
out["rights_invariant_violations"] = violations
out["rights_invariant_holds"] = not violations

# ------------------------------------------------------------ quarantine
quarantined = repo.candidates_in_state(State.RIGHTS_QUARANTINED)
codes = Counter()
with connect(cfg.database) as c:
    for q in quarantined:
        row = c.execute(
            "SELECT reason_codes FROM corpus_rights_decisions WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
            (q["candidate_id"],),
        ).fetchone()
        if row:
            for code in json.loads(row["reason_codes"] or "[]"):
                codes[code] += 1
out["quarantine_count"] = len(quarantined)
out["quarantine_reason_codes"] = dict(codes.most_common())

rejected = repo.candidates_in_state(State.RIGHTS_REJECTED)
reject_codes = Counter()
with connect(cfg.database) as c:
    for q in rejected:
        row = c.execute(
            "SELECT reason_codes FROM corpus_rights_decisions WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
            (q["candidate_id"],),
        ).fetchone()
        if row:
            for code in json.loads(row["reason_codes"] or "[]"):
                reject_codes[code] += 1
out["reject_count"] = len(rejected)
out["reject_reason_codes"] = dict(reject_codes.most_common())

# ----------------------------------------------------------- duplicates
out["duplicate_clusters"] = repo.duplicate_clusters()

# ---------------------------------------------------------------- disk
out["disk"] = {
    "corpus_bytes": storage.usage_bytes(),
    "corpus_human": human_bytes(storage.usage_bytes()),
    "blob_count": sum(1 for _ in storage.blobs.rglob("*") if _.is_file()) if storage.blobs.exists() else 0,
}

# --------------------------------------------------------- materialized
books_dir = cfg.books / "_auto"
manifesto_dir = cfg.manifestos / "_auto"
out["materialized"] = {
    "books": sorted(p.name for p in books_dir.glob("*.txt")) if books_dir.exists() else [],
    "manifesto": sorted(p.name for p in manifesto_dir.glob("*.txt")) if manifesto_dir.exists() else [],
}

# ------------------------------------------------------------- indexing
with connect(cfg.database) as c:
    out["index"] = {
        "harvested_sources": c.execute(
            "SELECT COUNT(*) n FROM sources WHERE manifest_id LIKE ?", (f"{MANIFEST_PREFIX}%",)
        ).fetchone()["n"],
        "harvested_chunks": c.execute(
            "SELECT COUNT(*) n FROM chunks c JOIN sources s ON s.id=c.source_id "
            "WHERE s.manifest_id LIKE ?", (f"{MANIFEST_PREFIX}%",)
        ).fetchone()["n"],
        "total_sources": c.execute("SELECT COUNT(*) n FROM sources").fetchone()["n"],
        "fts_orphans": c.execute(
            "SELECT COUNT(*) n FROM chunks_fts WHERE canonical_ref NOT IN (SELECT canonical_ref FROM chunks)"
        ).fetchone()["n"],
    }
out["indexed_document_ids"] = sorted(indexed_document_ids(cfg))

# ---------------------------------------------------------------- git
out["git"] = {
    "commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                             cwd=str(Path(__file__).resolve().parents[2])).stdout.strip(),
    "branch": subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True,
                             cwd=str(Path(__file__).resolve().parents[2])).stdout.strip(),
}

(Path(__file__).resolve().parents[2] / "reports").mkdir(exist_ok=True)
target = Path(__file__).resolve().parents[2] / "reports" / "CORPUS_HARVESTER_ACCEPTANCE.json"
target.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

print(f"wrote {target}")
print(f"  indexed={out['indexed_count']} quarantined={out['quarantine_count']} rejected={out['reject_count']}")
print(f"  rights invariant holds: {out['rights_invariant_holds']}")
print(f"  languages: {out['distributions']['language']}")
print(f"  destinations: {out['distributions']['destination']}")
print(f"  fts orphans: {out['index']['fts_orphans']}")
