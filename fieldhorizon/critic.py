from __future__ import annotations

import logging

from .config import AppConfig
from .interpreter import extract_json_object
from .llm import call_ollama

logger = logging.getLogger(__name__)


CRITIC_SYSTEM = """You are FIELD_HORIZON_CRITIC.

You are not a chat assistant.
You are not a creative writer.
You are a severe doctrinal and structural evaluator for Field Horizon.

Your function:
- Diagnose weaknesses in a generated Field Horizon fragment.
- Identify generic prose, weak metaphysics, bad structure, shallow axiom usage, and bad reference behavior.
- Produce rewrite directives that can be used by a separate rewriting engine.

You must be precise, harsh, technical, and useful.
Do not flatter the fragment.
Do not produce a rewritten fragment.
Do not chat.
"""


CRITIC_PROMPT = """Evaluate the response below according to the Field Horizon standard.

A valid Field Horizon fragment must:
- Contain exactly FIELD_FRAGMENT and METADATA_JSON.
- Develop a dense theological, metaphysical, apocalyptic, anti-modern synthesis.
- Use retrieved book fragments as symbolic/canonical material.
- Use retrieved JSON axioms as doctrinal constraints, not decorative citations.
- Use internal canon fragments as inherited doctrine, not copied material.
- Preserve source_refs, json_refs, and canon_refs exactly.
- Avoid generic AI-dark prose.
- Avoid internet clichés.
- Avoid slogans.
- Avoid merely listing query terms.
- Be 250 to 500 words.
- Contain 3 to 5 paragraphs.
- Have concrete symbolic development.

Evaluate:
1. Structure
2. Doctrinal force
3. Axiom development
4. Source integration
5. Reference discipline
6. Style failure
7. Canon fitness

Return exactly:

CRITIQUE
<5 to 10 concise paragraphs of precise criticism>

FAILURE_POINTS
- point 1
- point 2
- point 3
- point 4
- point 5

REWRITE_DIRECTIVES
- directive 1
- directive 2
- directive 3
- directive 4
- directive 5
- directive 6
- directive 7

CANON_FITNESS
verdict: CANON | USEFUL_FRAGMENT | HERESY | NOISE
reason: <one concise reason>
"""


def critique_response(
    cfg: AppConfig,
    original_prompt: str,
    response: str,
    model: str | None = None,
) -> str:
    prompt = f"""
{CRITIC_SYSTEM}

{CRITIC_PROMPT}

ORIGINAL_FIELD_HORIZON_PROMPT:
{original_prompt}

RESPONSE_TO_CRITIQUE:
{response}

BEGIN CRITIQUE NOW.
""".strip()

    try:
        return call_ollama(cfg, prompt, model=model)
    except Exception as exc:
        logger.warning("Critique generation failed: %s", exc)
        return ""


AXIOM_CRITIC_SYSTEM = """You are FIELD_HORIZON_AXIOM_CRITIC.

You are not a chat assistant.
You are not a creative writer.
You judge whether a candidate axiom, generated from an ontological pressure
edge between two opposed doctrines, is a genuine doctrinal mutation worth
promoting into canon.

A PROMOTE-worthy candidate:
- Is a real synthesis: something neither the source nor the target position
  could accept, but that both, followed to their logical end, imply.
- Is severe, precise, and doctrinally load-bearing.
- Is not a restatement, average, or watered-down compromise of either side.
- Is not a platitude that could apply to any two opposed ideas.

You must be precise, harsh, and useful. Do not flatter the candidate.
"""


AXIOM_CRITIC_PROMPT = """Judge the CANDIDATE_AXIOM below.

SOURCE_STATEMENT ({source_category}):
{source_statement}

TARGET_STATEMENT ({target_category}):
{target_statement}

OPPOSITION_REASON: {edge_reason}

CANDIDATE_AXIOM:
{candidate_statement}

Return JSON ONLY, exactly this shape, no markdown, no commentary:
{{
  "verdict": "PROMOTE" or "REJECT",
  "reason": "one concise sentence"
}}
"""


def critique_axiom_candidate(
    cfg: AppConfig,
    candidate_statement: str,
    source_statement: str,
    target_statement: str,
    edge_reason: str,
    source_category: str = "",
    target_category: str = "",
    model: str | None = None,
) -> dict:
    """
    Promotion gate for LLM-generated axiom candidates (review's real-upgrade
    tier §4): runs at temperature 0 since the output is a structured
    verdict, not prose (review's real-upgrade tier §5). On any failure
    (unreachable model, unparseable response) the candidate is rejected
    rather than silently promoted -- a broken critic must fail closed.
    """
    prompt = (
        AXIOM_CRITIC_SYSTEM
        + "\n\n"
        + AXIOM_CRITIC_PROMPT.format(
            source_category=source_category or "unknown",
            source_statement=source_statement,
            target_category=target_category or "unknown",
            target_statement=target_statement,
            edge_reason=edge_reason,
            candidate_statement=candidate_statement,
        )
    ).strip()

    try:
        raw = call_ollama(cfg, prompt, model=model, options={"temperature": 0})
        data = extract_json_object(raw)
    except Exception as exc:
        logger.warning("Axiom critic call failed, rejecting candidate: %s", exc)
        return {"verdict": "REJECT", "reason": f"critic call failed: {exc}"}

    verdict = str(data.get("verdict", "REJECT")).strip().upper()
    if verdict not in {"PROMOTE", "REJECT"}:
        verdict = "REJECT"

    return {"verdict": verdict, "reason": str(data.get("reason", "")).strip()}