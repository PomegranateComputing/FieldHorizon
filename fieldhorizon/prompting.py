from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from .config import AppConfig
from .fragments import MAX_WORDS, MIN_WORDS

SYSTEM_PROMPT = """
You are FIELD_HORIZON, a local canonical synthesis engine.

You do not chat.
You do not ask questions.
You do not request more context.
You always generate the requested fragment from the supplied material.

Your output is prose only: dense theological, metaphysical, apocalyptic, anti-modern prose.
Do not output METADATA_JSON.
Do not output headings, labels, or structured data.
Do not name or list your own references; if you mention a source, mention it narratively.

Treat sources as symbolic/canonical material for a fictional philosophical engine.
Do not quote long passages.
Do not claim certainty about sacred doctrine.
Synthesize. Do not summarize.
""".strip()


def clip(text: str, max_chars: int = 900) -> str:
    text = " ".join(str(text).split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " ⟨cut⟩"


def build_prompt(
    cfg: AppConfig,
    query: str,
    book_rows: Sequence[sqlite3.Row | dict],
    json_rows: Sequence[sqlite3.Row | dict],
    canon_rows: list[dict] | None = None,
) -> str:
    canon_rows = canon_rows or []

    books = [
        f"REF: {row['canonical_ref']} | TYPE: {row['source_type']}\n"
        f"TEXT: {clip(row['content'], 900)}"
        for row in book_rows
    ]

    axioms = [
        f"REF: {row['id']} | TRADITION: {row['tradition']} | CATEGORY: {row['category']} | SEVERITY: {row['severity']}\n"
        f"STATEMENT: {row['statement']}\n"
        f"GLOSS: {row['gloss']}"
        for row in json_rows
    ]

    canon_fragments = [
        f"REF: {row['ref']} | TYPE: internal_canon\n"
        f"TEXT: {clip(row['content'], 1200)}"
        for row in canon_rows
    ]

    return f"""
OUTPUT CONTRACT:
You must answer now.
Do not ask for a query.
Do not ask for references.
Use the query and references below.
Write one prose-only FIELD_FRAGMENT body.
Do not write FIELD_FRAGMENT or METADATA_JSON as labels.
Do not output metadata, headings, or bullet lists.

MODE: {cfg.mode}
TONE: {cfg.tone}
QUERY: {query}

TASK:
Generate one Field Horizon cycle fragment.
The fragment must synthesize the canonical fragments, structured axioms, and internal canon fragments.
It must not be a summary.
It must not name or list its own references as metadata.
It must be severe, precise, metaphysical, anti-modern, and apocalyptic.

STRICT GENERATION CONSTRAINTS:
- The fragment must be {MIN_WORDS} to {MAX_WORDS} words.
- The fragment must contain 3 to 5 paragraphs.
- Each retrieved JSON axiom must be developed into the prose, not merely named.
- Internal canon fragments must influence the synthesis without being copied.
- Do not compress all concepts into one sentence.
- Do not produce a slogan, abstract, summary, or outline.
- Do not simply list the query terms.
- Prefer concrete symbolic development over keyword accumulation.
- End with a complete sentence.

CANONICAL_BOOK_FRAGMENTS:
{chr(10).join(books) if books else '[none retrieved]'}

STRUCTURED_JSON_AXIOMS:
{chr(10).join(axioms) if axioms else '[none retrieved]'}

INTERNAL_CANON_FRAGMENTS:
{chr(10).join(canon_fragments) if canon_fragments else '[none retrieved]'}

BEGIN PROSE ONLY. BEGIN OUTPUT NOW.
""".strip()
