from __future__ import annotations

import logging

from .config import AppConfig
from .llm import call_ollama
from .ontology_spec import get_ontology

logger = logging.getLogger(__name__)


REWRITE_SYSTEM = """You are FIELD_HORIZON_REWRITER.

You are not a chat assistant.
You rewrite weak Field Horizon fragments into stronger canonical fragments.

Your job:
- Use the critique.
- Preserve the original prompt constraints.
- Preserve exact references.
- Improve depth, structure, severity, and symbolic force.
"""


REWRITE_PROMPT = """Rewrite the failed or weak Field Horizon response.

Rules:
- Return exactly FIELD_FRAGMENT and METADATA_JSON.
- FIELD_FRAGMENT must be 250 to 500 words.
- FIELD_FRAGMENT must contain 3 to 5 paragraphs.
- Develop every retrieved JSON axiom into prose.
- Use internal canon fragments as inherited doctrine.
- Do not merely list concepts.
- Do not use generic dark AI prose.
- Preserve exact source_refs, json_refs, and canon_refs from the original prompt.
- Do not invent references.
- Do not add slashes.
- Do not swap reference arrays.
- Make the prose darker, stranger, more doctrinal, more precise.
- Every JSON axiom category must appear as a developed motif.
- For each JSON axiom, include at least one concrete sentence that expresses its statement.
- Do not merely preserve refs; transform each axiom into a paragraph-level force.
- If doctrinal_enforcement was weak, prioritize axiom development over style.
- First identify all JSON axiom CATEGORY values from the original prompt.
- The rewritten FIELD_FRAGMENT must materially develop every CATEGORY.
- Each CATEGORY must appear through at least one concrete sentence, not just a keyword.
- Use exact category terms or close lexical forms when possible.
"""


def category_directives(json_rows: list[dict] | None) -> str:
    """
    Build the "if categories include X, explicitly develop ..." section
    dynamically from the categories actually present in this cycle's
    retrieved axioms (review §4), instead of a static block covering every
    category regardless of whether it was retrieved.
    """
    if not json_rows:
        return ""

    ontology = get_ontology()
    directives: list[str] = []

    for row in json_rows:
        category = str(row.get("category", "")).strip().lower()
        if not category:
            continue

        directive = ontology.rewrite_directive_for(category)
        if directive and directive not in directives:
            directives.append(directive)

    if not directives:
        return ""

    lines = "\n".join(f"- {directive}" for directive in directives)
    return f"CATEGORY DEVELOPMENT DIRECTIVES:\n{lines}"


def rewrite_response(
    cfg: AppConfig,
    original_prompt: str,
    original_response: str,
    critique: str,
    model: str | None = None,
    json_rows: list[dict] | None = None,
) -> str:
    directives = category_directives(json_rows)

    prompt = f"""
{REWRITE_SYSTEM}

{REWRITE_PROMPT}
{directives}

ORIGINAL_FIELD_HORIZON_PROMPT:
{original_prompt}

FAILED_OR_WEAK_RESPONSE:
{original_response}

CRITIQUE_AND_DIRECTIVES:
{critique}

BEGIN REWRITE NOW.
""".strip()

    try:
        return call_ollama(cfg, prompt, model=model)
    except Exception as exc:
        logger.warning("Rewrite generation failed: %s", exc)
        return ""