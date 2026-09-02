#!/usr/bin/env python3
"""
Re-estimate per-item severity, mutation_potential, and targets for
culture_war.json (review's real-upgrade tier §6).

Every entry in culture_war.json currently shares the same severity (0.72),
mutation_potential (0.66), and a batch-copied targets list (both hardcoded
in tools/convert_sentences_to_axioms.py regardless of the item's actual
content) -- flat metadata that adds a constant to json_score() and
doctrinal-enforcement scoring instead of a real signal.

This script sends each item's STATEMENT and GLOSS (both left completely
unchanged) to the LLM at temperature 0 and asks for a per-item estimate of
severity, mutation_potential, and targets. Output is written to a NEW
file, <source>_v2.json, next to the source file -- the source file is
never modified.

This script does not run on its own and is never invoked automatically by
the rest of the pipeline. Run it explicitly:

    python scripts/redistill_metadata.py
    python scripts/redistill_metadata.py --source data/json_corpus/culture_war.json --model hermes3:8b
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from fieldhorizon.config import load_config
from fieldhorizon.interpreter import extract_json_object
from fieldhorizon.llm import call_ollama

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("redistill_metadata")


DISTILL_SYSTEM = """You are FIELD_HORIZON_METADATA_DISTILLER.

You are not a chat assistant. You do not rewrite prose.

You are given one already-distilled doctrinal STATEMENT and the raw
polemical GLOSS it was distilled from. Estimate, for this item alone (not
for the batch or category it belongs to):

- severity: 0.0-1.0, how doctrinally severe and uncompromising this
  specific statement is.
- mutation_potential: 0.0-1.0, how much further doctrinal pressure this
  specific statement is likely to generate against other doctrines.
- targets: 2 to 4 short doctrinal target concepts (snake_case) that this
  item's own statement and gloss actually imply -- not a list copied from
  any other item.
"""

DISTILL_PROMPT = """STATEMENT: {statement}
GLOSS: {gloss}

Return JSON ONLY, exactly this shape, no markdown, no commentary:
{{
  "severity": 0.0,
  "mutation_potential": 0.0,
  "targets": ["...", "..."]
}}
"""


def _clamped(value: object, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def redistill_item(cfg, item: dict, model: str | None = None) -> dict:
    """
    Returns a copy of `item` with severity/mutation_potential/targets
    replaced by a per-item LLM estimate. statement and gloss are read but
    never written back changed. On any failure (unreachable model,
    unparseable response) the item's original metadata is kept unchanged
    rather than silently zeroed out.
    """
    prompt = (
        DISTILL_SYSTEM
        + "\n\n"
        + DISTILL_PROMPT.format(
            statement=item.get("statement", ""),
            gloss=item.get("gloss", ""),
        )
    ).strip()

    try:
        raw = call_ollama(cfg, prompt, model=model, options={"temperature": 0})
        data = extract_json_object(raw)
    except Exception as exc:
        logger.warning(
            "Redistillation failed for %s, keeping original metadata: %s",
            item.get("id", "?"),
            exc,
        )
        return dict(item)

    targets = data.get("targets")
    if not isinstance(targets, list) or not targets:
        targets = item.get("targets", [])

    new_item = dict(item)
    new_item["severity"] = _clamped(data.get("severity"), item.get("severity", 0.5))
    new_item["mutation_potential"] = _clamped(
        data.get("mutation_potential"), item.get("mutation_potential", 0.5)
    )
    new_item["targets"] = [str(t) for t in targets]
    return new_item


def redistill_corpus(cfg, items: list[dict], model: str | None = None) -> list[dict]:
    redistilled = []
    for index, item in enumerate(items, start=1):
        logger.info("[%d/%d] %s", index, len(items), item.get("id", "?"))
        redistilled.append(redistill_item(cfg, item, model=model))
    return redistilled


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--source",
        default=None,
        help="Path to culture_war.json (default: <json_corpus>/culture_war.json from config.yaml)",
    )
    parser.add_argument("--model", default=None, help="Override the configured default model")
    args = parser.parse_args()

    cfg = load_config(args.config)
    source = Path(args.source) if args.source else cfg.json_corpus / "culture_war.json"

    if not source.exists():
        logger.error("Source file not found: %s", source)
        raise SystemExit(1)

    items = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        logger.error("Expected a JSON list in %s", source)
        raise SystemExit(1)

    out_path = source.with_name(source.stem + "_v2.json")

    logger.info(
        "Redistilling %d item(s) from %s (model=%s) -> %s",
        len(items),
        source,
        args.model or cfg.default_model,
        out_path,
    )

    redistilled = redistill_corpus(cfg, items, model=args.model)

    out_path.write_text(json.dumps(redistilled, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote %s -- source file untouched: %s", out_path, source)


if __name__ == "__main__":
    main()
