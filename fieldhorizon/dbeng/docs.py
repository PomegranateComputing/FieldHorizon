"""
Identified documentary evidence: exact sentences from the PostgreSQL
documentation that justify how changes are executed, each verified at run
time against the HTML documentation shipped with the server binaries in
use. Where that documentation is not available locally, the item is still
identified (page and sentence) but marked unverified -- never presented as
checked.
"""
from __future__ import annotations

import html
import os
import re
from pathlib import Path

ENV_PG_DOCS = "FIELDHORIZON_DBENG_PG_DOCS"

DOCUMENTARY_ITEMS = (
    {
        "doc_id": "pg-createindex-no-transaction-block",
        "page": "sql-createindex.html",
        "section": "Building Indexes Concurrently",
        "quote": "Another difference is that a regular CREATE INDEX command can be performed within a transaction block, "
                 "but CREATE INDEX CONCURRENTLY cannot.",
        "used_for": "the operator connection runs CREATE INDEX CONCURRENTLY in autocommit mode",
    },
    {
        "doc_id": "pg-createindex-waits-for-transactions",
        "page": "sql-createindex.html",
        "section": "Building Indexes Concurrently",
        "quote": "When this option is used, PostgreSQL must perform two scans of the table, and in addition it must wait "
                 "for all existing transactions that could potentially modify or use the index to terminate.",
        "used_for": "the policy blocks execution while a long transaction is open (no_long_transactions)",
    },
    {
        "doc_id": "pg-createindex-invalid-index-recovery",
        "page": "sql-createindex.html",
        "section": "Building Indexes Concurrently",
        "quote": "The recommended recovery method in such cases is to drop the index and try again to perform "
                 "CREATE INDEX CONCURRENTLY.",
        "used_for": "an invalid index left by an interrupted build is dropped before any retry, and by db recover",
    },
)


def _normalise(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\s+", " ", text).strip()


def documentary_evidence(docs_dir: str | Path | None = None) -> list[dict]:
    root = Path(docs_dir) if docs_dir else (Path(os.environ[ENV_PG_DOCS]) if os.environ.get(ENV_PG_DOCS) else None)
    cache: dict[str, str | None] = {}
    items = []
    for item in DOCUMENTARY_ITEMS:
        page = item["page"]
        if page not in cache:
            path = root / page if root else None
            cache[page] = _normalise(path.read_text(encoding="utf-8")) if path and path.exists() else None
        body = cache[page]
        if body is None or root is None:
            status, detail = "unverified", "PostgreSQL HTML documentation not available locally"
        elif item["quote"] in body:
            status, detail = "verified", f"sentence found verbatim in {root / page}"
        else:
            status, detail = "not_found", f"sentence NOT found in {root / page} (different server version?)"
        items.append({**item, "status": status, "detail": detail})
    return items
