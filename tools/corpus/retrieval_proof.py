"""
Prove that a harvested document is retrievable through Field Horizon's
own retrieval path, and that its provenance survives the trip.

Runs the real `search_books` / `hybrid_search_chunks` used by the engine,
then enriches with `attach_corpus_provenance` exactly as a caller would.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fieldhorizon.config import load_config
from fieldhorizon.corpus.models import State
from fieldhorizon.corpus.repository import CorpusRepository
from fieldhorizon.db import connect
from fieldhorizon.retrieval import attach_corpus_provenance, search_books

cfg = load_config(str(Path(__file__).resolve().parents[2] / "config.yaml"))
repo = CorpusRepository(cfg.database)

indexed = repo.items_in_states([State.INGESTED, State.INDEXED])
if not indexed:
    print("NO INDEXED DOCUMENTS -- nothing to prove")
    sys.exit(1)

print(f"{len(indexed)} harvested documents in the index\n")

# Pick a distinctive phrase from a real harvested document, so the search
# is genuinely retrieving THAT document rather than matching anything.
target = indexed[0]
with connect(cfg.database) as c:
    row = c.execute(
        "SELECT c.content, c.canonical_ref FROM chunks c JOIN sources s ON s.id=c.source_id "
        "WHERE s.manifest_id = ? ORDER BY c.chunk_index LIMIT 1",
        (f"corpus:{target.document_id}",),
    ).fetchone()

if row is None:
    print(f"target {target.document_id} has no chunks")
    sys.exit(1)

words = [w for w in row["content"].split() if len(w) > 6][:6]
query = " ".join(words)
print(f"target document : {target.title}")
print(f"query           : {query[:100]}\n")

results = search_books(cfg, query, limit=5)
print(f"search_books returned {len(results)} chunk(s)")

enriched = attach_corpus_provenance(cfg, results)

found_target = False
for i, r in enumerate(enriched, 1):
    prov = r.get("provenance")
    ref = r.get("canonical_ref", "")
    if prov:
        if prov["document_id"] == target.document_id:
            found_target = True
        print(f"\n  [{i}] {ref[:70]}")
        print(f"      untrusted_content : {r['untrusted_content']}")
        print(f"      notice            : {r['untrusted_content_notice'][:70]}...")
        print(f"      document_id       : {prov['document_id']}")
        print(f"      title             : {prov['title'][:60]}")
        print(f"      author            : {prov['author'][:50]}")
        print(f"      destination       : {prov['destination']}")
        print(f"      source            : {prov['source']}")
        print(f"      canonical_url     : {prov['canonical_url']}")
        print(f"      license           : {prov['license']}")
        print(f"      scope             : {prov['distribution_scope']}")
        print(f"      attribution req.  : {prov['attribution_required']}")
        print(f"      provenance sha256 : {prov['provenance_sha256'][:24]}...")
    else:
        print(f"\n  [{i}] {ref[:70]}  (hand-curated -- no provenance key, as expected)")

print()
print(f"RETRIEVED THE TARGET DOCUMENT       : {found_target}")
print(f"ALL HARVESTED RESULTS MARKED UNTRUSTED: "
      f"{all(r.get('untrusted_content') for r in enriched if r.get('provenance'))}")

summary = {
    "target_document_id": target.document_id,
    "target_title": target.title,
    "query": query,
    "results": len(enriched),
    "harvested_results": sum(1 for r in enriched if r.get("provenance")),
    "retrieved_target": found_target,
    "provenance_fields_present": sorted(enriched[0]["provenance"]) if enriched and enriched[0].get("provenance") else [],
}
print("\nJSON:", json.dumps(summary, ensure_ascii=False))
