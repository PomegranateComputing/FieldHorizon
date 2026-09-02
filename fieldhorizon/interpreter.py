from __future__ import annotations

import json
import re

from .config import AppConfig
from .llm import call_ollama

INTERPRETER_SYSTEM = """
You are FIELD_HORIZON_SEMANTIC_INTERPRETER.

You are not a writer.
You are not a chat assistant.
You are a semantic analyst.

Your task:
Convert a raw user query into structured conceptual retrieval data.

Rules:
- Return valid JSON only.
- No markdown.
- No commentary.
- No thinking.
- Correct obvious typos silently.
- Interpret every important term.
- Expand literal words into symbolic, metaphysical, political, technological, and theological concepts.
- Do not generate prose.
""".strip()


def extract_json_object(text: str) -> dict:
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in interpreter output: {text[:300]}")

    depth = 0
    in_string = False
    escaped = False

    for idx in range(start, len(text)):
        char = text[idx]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : idx + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    break

    raise ValueError(f"No JSON object found in interpreter output: {text[:300]}")


def fallback_interpretation(query: str) -> dict:
    return {
        "literal_terms": query.split(),
        "corrected_query": query,
        "concepts": [],
        "ontology_targets": [],
        "retrieval_query": query,
        "source_bias": [],
        "notes": ["fallback_interpretation_used"],
    }


def interpret_query(
    cfg: AppConfig,
    query: str,
    model: str | None = None,
) -> dict:
    interpreter_model = model or "qwen3:8b"

    prompt = f"""
{INTERPRETER_SYSTEM}

RAW_QUERY:
{query}

Return exactly this JSON shape:
{{
  "literal_terms": ["..."],
  "corrected_query": "...",
  "term_interpretations": {{
    "term": ["concept1", "concept2"]
  }},
  "concepts": ["..."],
  "ontology_targets": ["logos", "modernity", "apocalypse", "bureaucracy", "tawhid", "godel_engine", "manifesto"],
  "source_bias": ["red_flags", "sacred_quran", "sacred_bible", "manifesto"],
  "retrieval_query": "expanded conceptual query string",
  "interpretive_summary": "one concise sentence"
}}

JSON ONLY.
""".strip()

    try:
        # Structured JSON output, not prose -- runs at temperature 0
        # (review's real-upgrade tier §5), unlike the agent/synthesizer
        # calls which keep the configured high creative temperature.
        raw = call_ollama(cfg, prompt, model=interpreter_model, options={"temperature": 0})
        data = extract_json_object(raw)
    except Exception:
        data = fallback_interpretation(query)

    if not isinstance(data, dict):
        data = fallback_interpretation(query)

    if not data.get("retrieval_query"):
        data["retrieval_query"] = query

    if not data.get("corrected_query"):
        data["corrected_query"] = query

    return data


def interpretation_to_prompt(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
