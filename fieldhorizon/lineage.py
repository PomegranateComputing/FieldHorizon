from __future__ import annotations

import json

from rich.tree import Tree

from .config import AppConfig
from .db import connect

# tools/convert_sentences_to_axioms.py writes glosses in exactly this shape
# for every distilled corpus entry; used to recover the raw material a
# statement was distilled from.
RAW_GLOSS_PREFIX = "Distilled from raw polemical sentence:"


class LineageNotFoundError(LookupError):
    pass


def load_entry(cfg: AppConfig, axiom_id: str) -> dict | None:
    with connect(cfg.database) as conn:
        row = conn.execute(
            "SELECT id, category, statement, gloss, raw_json FROM json_entries WHERE id = ?",
            (axiom_id,),
        ).fetchone()

    if row is None:
        return None

    entry = {key: row[key] for key in row.keys()}  # noqa: SIM118
    try:
        entry["raw"] = json.loads(row["raw_json"]) or {}
    except (json.JSONDecodeError, TypeError):
        entry["raw"] = {}

    return entry


def extract_raw_material(gloss: str) -> str | None:
    if RAW_GLOSS_PREFIX in gloss:
        return gloss.split(RAW_GLOSS_PREFIX, 1)[1].strip()
    return None


def resolve_raw_material_location(cfg: AppConfig, raw_material: str) -> dict | None:
    """
    Best-effort: locate the ingested book/manifesto chunk (if any) whose
    content contains this raw material text verbatim, and return its
    source title, chunk id, and exact character range into the original
    source file (Civilization Engine deep-provenance phase). Distilled
    corpus raw material (data/raw_sentences/*.txt) and ingested book
    chunks are different corpora, so this frequently finds nothing --
    that omission is expected, not an error.
    """
    snippet = (raw_material or "").strip()
    if not snippet:
        return None

    with connect(cfg.database) as conn:
        rows = conn.execute(
            """
            SELECT c.id AS chunk_id, c.content AS content, c.char_start AS char_start,
                   s.title AS source_title
            FROM chunks c
            JOIN sources s ON s.id = c.source_id
            WHERE c.char_start IS NOT NULL
            """
        ).fetchall()

    for row in rows:
        position = row["content"].find(snippet)
        if position == -1:
            continue

        return {
            "source_title": row["source_title"],
            "chunk_id": row["chunk_id"],
            "char_start": row["char_start"] + position,
            "char_end": row["char_start"] + position + len(snippet),
        }

    return None


def _add_axiom_branch(node: Tree, cfg: AppConfig, label: str, axiom_id: str | None, visited: set[str]) -> None:
    if not axiom_id:
        node.add(f"[red]{label}[/red]: unknown")
        return

    if axiom_id in visited:
        node.add(f"[red]{label}[/red] `{axiom_id}` -- cycle detected in provenance, stopping")
        return

    entry = load_entry(cfg, axiom_id)
    if entry is None:
        node.add(f"[red]{label}[/red] `{axiom_id}` -- not found in json_entries (never ingested?)")
        return

    branch = node.add(
        f"[green]{label}[/green] `{entry['id']}` ({entry['category']}): {entry['statement']}"
    )

    raw_material = extract_raw_material(entry.get("gloss") or "")
    if raw_material:
        location = resolve_raw_material_location(cfg, raw_material)
        if location:
            branch.add(
                f"[dim]raw material -- {raw_material}[/dim]\n"
                f"[dim]  source: {location['source_title']} / chunk `{location['chunk_id']}` "
                f"[{location['char_start']}:{location['char_end']}][/dim]"
            )
        else:
            branch.add(f"[dim]raw material -- {raw_material}[/dim]")

    provenance = entry["raw"].get("provenance")
    if provenance:
        _add_provenance_children(branch, cfg, provenance, visited | {axiom_id})


def _add_provenance_children(node: Tree, cfg: AppConfig, provenance: dict, visited: set[str]) -> None:
    stratum = provenance.get("stratum")
    stratum_label = f"stratum {stratum:04d}" if isinstance(stratum, int) else "stratum unknown"
    stratum_node = node.add(f"[cyan]{stratum_label}[/cyan]")

    for edge in provenance.get("source_edges", []):
        edge_node = stratum_node.add(
            f"[yellow]pressure edge[/yellow] "
            f"{edge.get('source_category')} vs {edge.get('target_category')} "
            f"(score={edge.get('pressure_score')}) -- {edge.get('reason')}"
        )

        _add_axiom_branch(edge_node, cfg, "source", edge.get("source_id"), visited)
        _add_axiom_branch(edge_node, cfg, "target", edge.get("target_id"), visited)

    source_cycle_ids = provenance.get("source_cycle_ids") or []
    if source_cycle_ids:
        canon_node = node.add("[magenta]canon genealogy (source axioms trace to)[/magenta]")
        for cycle_id in source_cycle_ids:
            _add_cycle_genealogy_branch(canon_node, cfg, int(cycle_id), set())


def build_axiom_lineage_tree(cfg: AppConfig, axiom_id: str) -> Tree:
    """
    Walks provenance from a generated axiom through its pressure edges down
    to the source axioms it was mutated from, and -- where a source axiom's
    gloss cites a raw distilled sentence -- down to that raw material,
    resolved (where possible) to the exact ingested chunk and character
    range it lives at. Recurses through multiple strata: a stratum-2 axiom
    generated from a stratum-1 axiom resolves all the way down. Where a
    source axiom's own provenance names canon cycles it traces to (see
    ontology.export_generated_axioms's source_cycle_ids), the tree also
    branches into canon genealogy for those cycles. Any axiom_id (not
    just a generated one) can be the root; it just won't have a
    provenance branch to walk if it was never LLM-generated.
    """
    entry = load_entry(cfg, axiom_id)
    if entry is None:
        raise LineageNotFoundError(f"No json_entries row found for id={axiom_id!r}")

    root = Tree(f"[bold]{entry['id']}[/bold] ({entry['category']}): {entry['statement']}")

    raw_material = extract_raw_material(entry.get("gloss") or "")
    if raw_material:
        location = resolve_raw_material_location(cfg, raw_material)
        if location:
            root.add(
                f"[dim]raw material -- {raw_material}[/dim]\n"
                f"[dim]  source: {location['source_title']} / chunk `{location['chunk_id']}` "
                f"[{location['char_start']}:{location['char_end']}][/dim]"
            )
        else:
            root.add(f"[dim]raw material -- {raw_material}[/dim]")

    provenance = entry["raw"].get("provenance")
    if provenance:
        _add_provenance_children(root, cfg, provenance, {axiom_id})
    else:
        root.add("[dim](no provenance -- not an LLM-generated axiom)[/dim]")

    return root


def _load_cycle_row(cfg: AppConfig, cycle_id: int) -> dict | None:
    with connect(cfg.database) as conn:
        row = conn.execute(
            """
            SELECT id, query, verdict, final_score, parent_cycle_ids, retired_at, retirement_reason
            FROM cycles WHERE id = ?
            """,
            (cycle_id,),
        ).fetchone()

    if row is None:
        return None

    return {key: row[key] for key in row.keys()}  # noqa: SIM118


def _cycle_label(row: dict) -> str:
    label = f"cycle `{row['id']}` ({row['verdict']}, score={row['final_score']})"
    if row.get("retired_at"):
        reason = row.get("retirement_reason") or "unspecified"
        label = f"[strike red]{label}[/strike red] [red]\\[RETIRED: {reason}][/red]"
    return label


def _parent_ids(row: dict) -> list[int]:
    if not row.get("parent_cycle_ids"):
        return []
    try:
        ids = json.loads(row["parent_cycle_ids"]) or []
    except (json.JSONDecodeError, TypeError):
        return []
    return [int(i) for i in ids] if isinstance(ids, list) else []


def _add_cycle_genealogy_branch(node: Tree, cfg: AppConfig, cycle_id: int, visited: set[int]) -> None:
    if cycle_id in visited:
        node.add(f"[red]cycle `{cycle_id}`[/red] -- cycle detected in genealogy, stopping")
        return

    row = _load_cycle_row(cfg, cycle_id)
    if row is None:
        node.add(f"[red]cycle `{cycle_id}`[/red] -- not found")
        return

    branch = node.add(_cycle_label(row))
    for parent_id in _parent_ids(row):
        _add_cycle_genealogy_branch(branch, cfg, parent_id, visited | {cycle_id})


def build_cycle_genealogy_tree(cfg: AppConfig, cycle_id: int) -> Tree:
    """
    Walks a canon cycle's ancestry through parent_cycle_ids -- the cycle
    ids whose canon fragments were MMR-selected into this cycle's prompt
    at write time (Civilization Engine genealogy phase). Retired ancestors
    are marked, never hidden: retirement excludes a cycle from future MMR
    selection, not from the historical record of what actually shaped a
    later fragment.
    """
    row = _load_cycle_row(cfg, cycle_id)
    if row is None:
        raise LineageNotFoundError(f"No cycles row found for id={cycle_id!r}")

    root = Tree(f"{_cycle_label(row)}\nquery: {row['query']}")

    parent_ids = _parent_ids(row)
    if parent_ids:
        for parent_id in parent_ids:
            _add_cycle_genealogy_branch(root, cfg, parent_id, {cycle_id})
    else:
        root.add("[dim](no parent canon fragments recorded)[/dim]")

    return root


def build_lineage_tree(cfg: AppConfig, target_id: str) -> Tree:
    """
    Dual dispatch: a purely numeric `target_id` is a cycles.id and walks
    canon genealogy (build_cycle_genealogy_tree); anything else is a
    json_entries axiom id and walks pressure-edge provenance
    (build_axiom_lineage_tree), exactly as before this phase.
    """
    stripped = target_id.strip()
    if stripped.lstrip("-").isdigit():
        return build_cycle_genealogy_tree(cfg, int(stripped))

    return build_axiom_lineage_tree(cfg, target_id)
