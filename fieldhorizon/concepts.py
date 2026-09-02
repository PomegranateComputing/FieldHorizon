from __future__ import annotations

import logging
from dataclasses import dataclass

from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from .config import AppConfig
from .db import connect
from .interpreter import extract_json_object
from .llm import call_ollama
from .ontology_spec import get_ontology

logger = logging.getLogger(__name__)

MAX_MOTIFS = 3
_VALID_ENTITY_KINDS = {"person", "place", "work"}

SEMANTIC_KIND_DOMAIN = "domain"
SEMANTIC_KIND_ENTITY = "entity"
SEMANTIC_KIND_MOTIF = "motif"
SEMANTIC_KINDS = (SEMANTIC_KIND_DOMAIN, SEMANTIC_KIND_ENTITY, SEMANTIC_KIND_MOTIF)

# Table/column pairs are internal fixed constants (never external input),
# so interpolating them into SQL below is safe -- the same discipline
# temporal._canon_as_of documents for its own time_column parameter.
_TABLE_FOR_KIND = {
    SEMANTIC_KIND_DOMAIN: ("chunk_concepts", "domain"),
    SEMANTIC_KIND_ENTITY: ("chunk_entities", "entity"),
    SEMANTIC_KIND_MOTIF: ("chunk_motifs", "motif"),
}

_CONCEPT_PROMPT_TEMPLATE = """Read the passage below and extract, in JSON only:

1. "domains": which of the following doctrinal domains are meaningfully
   present in the passage. Use ONLY names from this exact list -- do not
   invent a domain name that isn't listed, and omit any that don't apply:
   {domain_list}
   For each domain present, give a confidence from 0.0 to 1.0.
2. "entities": named people, places, and works (books, doctrines,
   institutions) mentioned in the passage.
3. "motifs": up to {max_motifs} short symbolic phrases (2-5 words each)
   capturing recurring images or ideas in the passage -- not a summary,
   the recurring symbolic material itself.

Passage:
\"\"\"
{content}
\"\"\"

Return JSON ONLY, exactly this shape, no markdown, no commentary:
{{"domains": [{{"domain": "...", "confidence": 0.0}}], "entities": [{{"name": "...", "kind": "person|place|work"}}], "motifs": ["..."]}}
"""


def build_concept_prompt(content: str, domain_names: tuple[str, ...]) -> str:
    return _CONCEPT_PROMPT_TEMPLATE.format(
        domain_list=", ".join(domain_names), max_motifs=MAX_MOTIFS, content=content
    )


def tag_chunk_content(
    cfg: AppConfig, content: str, domain_names: tuple[str, ...], model: str | None = None
) -> dict:
    """
    One temperature-0 LLM call returning ontology domains present
    (rejecting anything not in `domain_names` -- a hallucinated domain name
    is dropped, never stored, so chunk_concepts can never disagree with the
    ontology about what domains exist), named entities, and up to
    MAX_MOTIFS symbolic motifs. Raises on an unreachable model or malformed
    JSON; callers decide whether to skip or fail the batch.
    """
    prompt = build_concept_prompt(content, domain_names)
    raw = call_ollama(cfg, prompt, model=model, options={"temperature": 0})
    data = extract_json_object(raw)

    valid_domains = set(domain_names)
    domains: list[dict] = []
    for item in data.get("domains", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("domain", "")).strip()
        if name not in valid_domains:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 1.0))))
        except (TypeError, ValueError):
            confidence = 1.0
        domains.append({"domain": name, "confidence": confidence})

    entities: list[dict] = []
    for item in data.get("entities", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        kind = str(item.get("kind", "")).strip().lower()
        if name and kind in _VALID_ENTITY_KINDS:
            entities.append({"name": name, "kind": kind})

    motifs = [
        str(m).strip() for m in data.get("motifs", []) if isinstance(m, str) and str(m).strip()
    ][:MAX_MOTIFS]

    return {"domains": domains, "entities": entities, "motifs": motifs}


def store_chunk_tags(cfg: AppConfig, chunk_id: int, tags: dict) -> None:
    with connect(cfg.database) as conn:
        conn.execute("DELETE FROM chunk_concepts WHERE chunk_id = ?", (chunk_id,))
        conn.execute("DELETE FROM chunk_entities WHERE chunk_id = ?", (chunk_id,))
        conn.execute("DELETE FROM chunk_motifs WHERE chunk_id = ?", (chunk_id,))

        for domain in tags["domains"]:
            conn.execute(
                "INSERT INTO chunk_concepts(chunk_id, domain, confidence) VALUES (?, ?, ?)",
                (chunk_id, domain["domain"], domain["confidence"]),
            )
        for entity in tags["entities"]:
            conn.execute(
                "INSERT OR IGNORE INTO chunk_entities(chunk_id, entity, kind) VALUES (?, ?, ?)",
                (chunk_id, entity["name"], entity["kind"]),
            )
        for motif in tags["motifs"]:
            conn.execute(
                "INSERT OR IGNORE INTO chunk_motifs(chunk_id, motif) VALUES (?, ?)",
                (chunk_id, motif),
            )

        conn.execute(
            "UPDATE chunks SET concepts_tagged_at = CURRENT_TIMESTAMP WHERE id = ?", (chunk_id,)
        )
        conn.commit()


def tag_chunks(cfg: AppConfig, max_chunks: int | None = None, show_progress: bool = True) -> int:
    """
    Batched, resumable concept/entity/motif tagging: only considers chunks
    with concepts_tagged_at IS NULL, so an interrupted or budget-capped run
    (--max-chunks) picks back up on the next untagged chunk rather than
    re-tagging already-processed ones. A per-chunk tagging failure (model
    unreachable, malformed JSON) is logged and skipped, not fatal to the
    batch.
    """
    domain_names = get_ontology().all_domain_names()

    query = "SELECT id, content FROM chunks WHERE concepts_tagged_at IS NULL ORDER BY id ASC"
    if max_chunks is not None:
        query += f" LIMIT {int(max_chunks)}"

    with connect(cfg.database) as conn:
        rows = conn.execute(query).fetchall()

    count = 0
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        disable=not show_progress,
    ) as progress:
        task = progress.add_task("Tagging chunks", total=len(rows))
        for row in rows:
            chunk_id = int(row["id"])
            try:
                tags = tag_chunk_content(cfg, row["content"], domain_names)
            except Exception as exc:
                logger.warning("Concept tagging failed for chunk %d: %s", chunk_id, exc)
                progress.advance(task)
                continue
            store_chunk_tags(cfg, chunk_id, tags)
            count += 1
            progress.advance(task)

    return count


@dataclass(frozen=True)
class SemanticNode:
    kind: str
    label: str
    count: int


def top_semantic_nodes(cfg: AppConfig, kind: str, limit: int = 20) -> list[SemanticNode]:
    """Most-frequent nodes of one kind -- a real, bounded starting point for exploration (FABLE Sec.10.8: never dump the whole graph)."""
    table, column = _TABLE_FOR_KIND[kind]
    with connect(cfg.database) as conn:
        rows = conn.execute(
            f"SELECT {column} AS label, COUNT(DISTINCT chunk_id) AS n FROM {table} GROUP BY {column} ORDER BY n DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [SemanticNode(kind=kind, label=row["label"], count=int(row["n"])) for row in rows]


@dataclass(frozen=True)
class SemanticNeighbor:
    kind: str
    label: str
    shared_chunk_count: int


@dataclass(frozen=True)
class SemanticNeighborhood:
    kind: str
    label: str
    neighbors: list[SemanticNeighbor]
    total_neighbor_count: int


def semantic_neighbors(cfg: AppConfig, kind: str, label: str, limit: int = 15) -> SemanticNeighborhood:
    """
    Co-occurrence neighborhood: every OTHER concept/entity/motif tagged on
    a chunk this node is also tagged on, ranked by shared chunk count.
    Progressive expansion (FABLE Sec.10.8) -- one node's real neighbors at
    a time, never the whole graph; total_neighbor_count reports the true
    count even when the returned list is capped, so the UI can show an
    honest "showing N of M" density warning instead of silently truncating.
    """
    table, column = _TABLE_FOR_KIND[kind]
    with connect(cfg.database) as conn:
        chunk_ids = [
            int(row["chunk_id"])
            for row in conn.execute(f"SELECT chunk_id FROM {table} WHERE {column} = ?", (label,)).fetchall()
        ]
        if not chunk_ids:
            return SemanticNeighborhood(kind=kind, label=label, neighbors=[], total_neighbor_count=0)

        placeholders = ",".join("?" for _ in chunk_ids)
        neighbors: list[SemanticNeighbor] = []
        for other_kind, (other_table, other_column) in _TABLE_FOR_KIND.items():
            rows = conn.execute(
                f"SELECT {other_column} AS label, COUNT(DISTINCT chunk_id) AS n FROM {other_table} "
                f"WHERE chunk_id IN ({placeholders}) GROUP BY {other_column}",
                chunk_ids,
            ).fetchall()
            for row in rows:
                if other_kind == kind and row["label"] == label:
                    continue  # never neighbor a node with itself
                neighbors.append(SemanticNeighbor(kind=other_kind, label=row["label"], shared_chunk_count=int(row["n"])))

    neighbors.sort(key=lambda n: n.shared_chunk_count, reverse=True)
    return SemanticNeighborhood(kind=kind, label=label, neighbors=neighbors[:limit], total_neighbor_count=len(neighbors))
