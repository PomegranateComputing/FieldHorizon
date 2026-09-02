from __future__ import annotations

import json
import logging

from .agents import AgentOutput
from .config import AppConfig
from .fragments import MAX_WORDS, MIN_WORDS, extract_field_fragment, fragment_is_usable
from .llm import call_ollama

logger = logging.getLogger(__name__)


def agent_outputs_to_text(agent_outputs: list[AgentOutput]) -> str:
    return "\n\n---\n\n".join(
        f"AGENT: {out.name}\nROLE: {out.role}\nMEMORANDUM:\n{out.content}"
        for out in agent_outputs
    )


def build_synthesis_prompt(
    base_prompt: str,
    agent_outputs: list[AgentOutput],
    json_domain: str | None = None,
    rust_pressure_text: str = "",
    interpretation_text: str = "",
    query: str | None = None,
    source_refs: list[str] | None = None,
    json_refs: list[str] | None = None,
    json_axioms: list[str] | None = None,
) -> str:
    agent_text = agent_outputs_to_text(agent_outputs)
    source_refs = source_refs or []
    json_refs = json_refs or []
    json_axioms = json_axioms or []
    query = query or base_prompt

    domain_focus = ""
    if json_domain == "godel_engine":
        domain_focus = "GÖDEL ENGINE: incompleteness, self-reference, closed systems, undecidable truth, outside necessity."

    expected_refs = ", ".join(source_refs[:6]) if source_refs else "[none]"
    json_axiom_text = "\n".join(f"- {item}" for item in json_axioms[:6]) if json_axioms else "- [none]"

    return f"""
You are FIELD_HORIZON_SYNTHESIZER.
Write one prose-only FIELD_FRAGMENT body.

Original query:
{query}

Semantic interpretation:
{interpretation_text if interpretation_text else "[none]"}

Expected refs:
{expected_refs}

JSON axioms:
{json_axiom_text}

Domain focus:
{domain_focus if domain_focus else "[none]"}

Rust pressure graph:
{rust_pressure_text if rust_pressure_text else "[none]"}

Agent memoranda:
{agent_text if agent_text else "[none]"}

Rules:
- {MIN_WORDS} to {MAX_WORDS} words.
- 3 to 5 paragraphs.
- Prose only. No metadata, no headings, no bullets, no field labels.
- Do not quote or copy agent text.
- Synthesize the retrieved materials into a severe, precise, doctrinal prose fragment.
- End with a complete sentence.
""".strip()


def generate_fragment(
    cfg: AppConfig,
    synthesis_prompt: str,
    model: str | None = None,
    attempts: int = 2,
) -> str:
    """
    Call the model for prose only (no metadata contract) and retry up to
    `attempts` times against fragment_is_usable. METADATA_JSON is never
    requested of the model; it is assembled in code by
    build_structured_response.

    call_ollama raises rather than returning "" on empty content or an
    unreachable server (review §5), so a failed attempt here is caught and
    logged and simply counts as a failed quality check -- this is the
    retry loop that raising was meant to feed.

    `attempts` defaults to 2, not 3 (review §8's "most of the retry
    scaffolding can shrink," once prose generation and code-generated
    metadata are properly separated -- which they now are). The old
    default of 3 compounded badly on a hard failure: call_ollama already
    retries transient connection errors/5xx up to its own MAX_ATTEMPTS
    with backoff before raising, so an unreachable Ollama server used to
    cost up to 3 outer attempts x 3 inner retries = 9 total HTTP attempts
    before generate_fragment gave up. The remaining retries here are for
    stochastic quality misses (too short, truncated) against an already-
    successful call, which is the only case retrying here still helps.
    """
    fragment = ""

    for attempt in range(1, attempts + 1):
        attempt_prompt = synthesis_prompt

        if attempt > 1:
            attempt_prompt = (
                synthesis_prompt
                + f"\n\nPREVIOUS OUTPUT FAILED QUALITY CHECK, ATTEMPT {attempt}.\n"
                + "Regenerate from scratch now.\n"
                + "Return ONLY the prose body.\n"
                + f"Write {MIN_WORDS} to {MAX_WORDS} words.\n"
                + "Use 3 to 5 complete paragraphs.\n"
                + "Do not write FIELD_FRAGMENT.\n"
                + "Do not write METADATA_JSON.\n"
                + "Do not stop abruptly.\n"
                + "End with a complete sentence.\n"
            )

        try:
            raw_response = call_ollama(cfg, attempt_prompt, model=model)
        except Exception as exc:
            logger.warning("Fragment generation attempt %d/%d failed: %s", attempt, attempts, exc)
            continue

        fragment = extract_field_fragment(raw_response)

        if fragment_is_usable(fragment):
            break

    return fragment


def build_structured_response(
    fragment_text: str,
    query: str,
    source_refs: list[str],
    json_refs: list[str],
    canon_refs: list[str],
    interpretation: dict | None = None,
    retrieval_query: str | None = None,
    dominant_traditions: list[str] | None = None,
    tags: list[str] | None = None,
) -> str:
    metadata = {
        "query": query,
        "retrieval_query": retrieval_query or query,
        "semantic_interpretation": interpretation or {},
        "dominant_traditions": dominant_traditions or ["recursive_canon"],
        "source_refs": source_refs,
        "json_refs": json_refs,
        "canon_refs": canon_refs,
        "doctrinal_pressure": 0.0,
        "mutation_potential": 0.0,
        "tags": tags or ["synthesis"],
    }

    return (
        "FIELD_FRAGMENT\n"
        f"{fragment_text.strip()}\n\n"
        "METADATA_JSON\n"
        f"{json.dumps(metadata, ensure_ascii=False, indent=2)}"
    )
