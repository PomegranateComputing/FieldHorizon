from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .db import connect
from .verdicts import VERDICT_DIRS


def build_cycle_index(cfg: AppConfig) -> Path:
    """
    The DB is the source of truth for canon (review §5): this is now a
    query over `cycles`, not a directory crawl regexing markdown logs.
    The .md files under outputs/<verdict>/ remain human-readable views,
    named `cycle_<id>.md` to match the id this index reports.
    """
    with connect(cfg.database) as conn:
        rows = conn.execute(
            """
            SELECT id, query, verdict, final_score, created_at
            FROM cycles
            WHERE dry_run = 0 AND verdict IS NOT NULL
            ORDER BY created_at DESC
            """
        ).fetchall()

    out = cfg.outputs / "CYCLE_INDEX.md"

    lines = [
        "# Field Horizon Cycle Index",
        "",
        "| Date | Verdict | Score | Query | File |",
        "|---|---:|---:|---|---|",
    ]

    for row in rows:
        verdict = row["verdict"] or "NOISE"
        verdict_dir = VERDICT_DIRS.get(verdict, "noise")
        query = (row["query"] or "").replace("|", "/")
        rel_path = f"{verdict_dir}/cycle_{row['id']}.md"
        lines.append(
            f"| {row['created_at']} | {verdict} | {row['final_score']} | {query} | `{rel_path}` |"
        )

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
