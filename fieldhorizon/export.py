from __future__ import annotations

from .config import AppConfig
from .db import connect


def export_codex(cfg: AppConfig) -> str:
    cfg.outputs.mkdir(parents=True, exist_ok=True)
    out = cfg.outputs / "FIELD_HORIZON_CODEX.md"
    with connect(cfg.database) as conn:
        rows = conn.execute(
            "SELECT id, query, model, response, created_at FROM cycles ORDER BY id DESC LIMIT 50"
        ).fetchall()
    parts = ["# FIELD HORIZON CODEX\n"]
    for row in reversed(rows):
        parts.append(f"\n---\n\n## Cycle {row['id']} | {row['created_at']} | {row['model']}\n")
        parts.append(f"**Query:** {row['query']}\n\n")
        parts.append(row["response"] + "\n")
    out.write_text("".join(parts), encoding="utf-8")
    return str(out)
