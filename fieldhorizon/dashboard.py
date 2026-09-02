from __future__ import annotations

import html
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import AppConfig
from .db import connect
from .schools import latest_schools
from .weather import DEFAULT_SURFACE_LIMIT, SCOPE_CANON, SCOPE_SURFACE, axis_history, corpus_weather

SPARK_CHARS = "▁▂▃▄▅▆▇█"  # ▁▂▃▄▅▆▇█


def render_sparkline(values: list[float]) -> str:
    if not values:
        return "(no history)"
    if len(values) == 1:
        return SPARK_CHARS[len(SPARK_CHARS) // 2]

    lo, hi = min(values), max(values)
    spread = hi - lo
    if spread == 0:
        mid = SPARK_CHARS[len(SPARK_CHARS) // 2]
        return mid * len(values)

    return "".join(SPARK_CHARS[min(len(SPARK_CHARS) - 1, int((v - lo) / spread * len(SPARK_CHARS)))] for v in values)


def _corpus_counts(cfg: AppConfig) -> dict[str, int]:
    with connect(cfg.database) as conn:
        rows = conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM cycles WHERE dry_run = 0 AND retired_at IS NULL GROUP BY verdict"
        ).fetchall()
    return {row["verdict"] or "unknown": int(row["n"]) for row in rows}


def _recent_councils(cfg: AppConfig, limit: int = 5) -> list[dict]:
    with connect(cfg.database) as conn:
        rows = conn.execute(
            "SELECT id, started_at, examined, overturned FROM councils ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def render_weather_tui(cfg: AppConfig, console: Console | None = None) -> None:
    console = console or Console()

    weather_table = Table(title="Doctrinal Weather")
    weather_table.add_column("Axis")
    weather_table.add_column("Surface history (recent -> now)")
    weather_table.add_column("Surface", justify="right")
    weather_table.add_column("Canon (deep)", justify="right")

    surface_readings = corpus_weather(cfg, scope=SCOPE_SURFACE, surface_limit=DEFAULT_SURFACE_LIMIT)
    canon_readings = corpus_weather(cfg, scope=SCOPE_CANON)
    histories = {h.axis: h.values for h in axis_history(cfg, SCOPE_SURFACE, limit=30)}

    for axis in sorted(set(surface_readings) | set(canon_readings) | set(histories)):
        weather_table.add_row(
            axis,
            render_sparkline(histories.get(axis, [])),
            f"{surface_readings[axis]:+.3f}" if axis in surface_readings else "-",
            f"{canon_readings[axis]:+.3f}" if axis in canon_readings else "-",
        )
    console.print(weather_table)

    schools = latest_schools(cfg)
    schools_table = Table(title="Active Schools of Thought")
    schools_table.add_column("Id")
    schools_table.add_column("Name")
    schools_table.add_column("Members", justify="right")
    schools_table.add_column("Lineage")
    for school in schools:
        lineage = f"formerly #{school.previous_school_id}" if school.previous_school_id else "-"
        schools_table.add_row(str(school.id), school.name, str(school.member_count), lineage)
    console.print(schools_table if schools else "[dim]No schools clustered yet -- run `field-horizon schools`.[/dim]")

    councils = _recent_councils(cfg)
    councils_table = Table(title="Recent Councils")
    councils_table.add_column("Id")
    councils_table.add_column("Started")
    councils_table.add_column("Examined", justify="right")
    councils_table.add_column("Overturned", justify="right")
    for council in councils:
        councils_table.add_row(
            str(council["id"]), str(council["started_at"]), str(council["examined"]), str(council["overturned"])
        )
    console.print(councils_table if councils else "[dim]No councils run yet.[/dim]")

    counts = _corpus_counts(cfg)
    summary = " | ".join(f"{verdict}: {n}" for verdict, n in sorted(counts.items()))
    console.print(f"\n{summary or '(no cycles yet)'}")


def _svg_sparkline(values: list[float], width: int = 200, height: int = 40) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    spread = hi - lo or 1.0
    step = width / (len(values) - 1)
    points = " ".join(
        f"{i * step:.1f},{height - ((v - lo) / spread * height):.1f}" for i, v in enumerate(values)
    )
    return f'<svg width="{width}" height="{height}"><polyline points="{points}" fill="none" stroke="currentColor" stroke-width="2"/></svg>'


def render_weather_html(cfg: AppConfig) -> str:
    surface_readings = corpus_weather(cfg, scope=SCOPE_SURFACE, surface_limit=DEFAULT_SURFACE_LIMIT)
    canon_readings = corpus_weather(cfg, scope=SCOPE_CANON)
    histories = {h.axis: h.values for h in axis_history(cfg, SCOPE_SURFACE, limit=30)}
    schools = latest_schools(cfg)
    councils = _recent_councils(cfg)
    counts = _corpus_counts(cfg)

    axis_rows = "\n".join(
        f"<tr><td>{html.escape(axis)}</td><td>{_svg_sparkline(histories.get(axis, []))}</td>"
        f"<td>{surface_readings.get(axis, float('nan')):+.3f}</td><td>{canon_readings.get(axis, float('nan')):+.3f}</td></tr>"
        for axis in sorted(set(surface_readings) | set(canon_readings) | set(histories))
    )

    school_rows = "\n".join(
        f"<tr><td>{s.id}</td><td>{html.escape(s.name)}</td><td>{s.member_count}</td>"
        f"<td>{html.escape(s.summary)}</td></tr>"
        for s in schools
    )

    council_rows = "\n".join(
        f"<tr><td>{c['id']}</td><td>{html.escape(str(c['started_at']))}</td>"
        f"<td>{c['examined']}</td><td>{c['overturned']}</td></tr>"
        for c in councils
    )

    counts_summary = " &middot; ".join(f"{html.escape(str(v))}: {n}" for v, n in sorted(counts.items()))

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Field Horizon -- Doctrinal Weather</title>
<style>
  body {{ background: #0b0b0d; color: #ddd; font-family: Georgia, serif; margin: 2rem; }}
  h1, h2 {{ color: #c9a34c; font-weight: normal; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
  th, td {{ border-bottom: 1px solid #333; padding: 0.4rem 0.8rem; text-align: left; }}
  th {{ color: #999; text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.05em; }}
  svg {{ color: #c9a34c; }}
</style>
</head>
<body>
<h1>Field Horizon &mdash; Doctrinal Weather</h1>
<p>{counts_summary or "(no cycles yet)"}</p>

<h2>Axes</h2>
<table>
<tr><th>Axis</th><th>Surface history</th><th>Surface</th><th>Canon (deep)</th></tr>
{axis_rows or '<tr><td colspan="4">No readings yet.</td></tr>'}
</table>

<h2>Active Schools of Thought</h2>
<table>
<tr><th>Id</th><th>Name</th><th>Members</th><th>Summary</th></tr>
{school_rows or '<tr><td colspan="4">No schools clustered yet.</td></tr>'}
</table>

<h2>Recent Councils</h2>
<table>
<tr><th>Id</th><th>Started</th><th>Examined</th><th>Overturned</th></tr>
{council_rows or '<tr><td colspan="4">No councils run yet.</td></tr>'}
</table>

</body>
</html>
"""


def write_weather_html(cfg: AppConfig, path: Path | None = None) -> Path:
    path = path or (cfg.outputs / "weather.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_weather_html(cfg), encoding="utf-8")
    return path
