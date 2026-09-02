from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

from fieldhorizon.config import AppConfig

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "redistill_metadata.py"
_spec = importlib.util.spec_from_file_location("redistill_metadata", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
redistill_metadata = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redistill_metadata)


def make_config(tmp_path) -> AppConfig:
    return AppConfig(
        root=tmp_path,
        database=tmp_path / "data.sqlite3",
        books=tmp_path / "books",
        json_corpus=tmp_path / "json_corpus",
        outputs=tmp_path / "outputs",
        logs=tmp_path / "logs",
        ollama_base_url="http://localhost:11434",
        default_model="hermes3:8b",
        temperature=1.25,
        top_p=0.95,
        repeat_penalty=1.08,
        num_ctx=8192,
        book_fragments=6,
        json_entries=8,
        chunk_chars=1800,
        chunk_overlap=250,
        tone="dark",
        mode="canonical_synthesis",
        manifestos=tmp_path / "manifestos",
        embedding_model="nomic-embed-text",
    )


ITEM = {
    "id": "culture_war_0001",
    "group_name": "culture_war",
    "category": "elite_capture",
    "tradition": "political_sociology",
    "statement": "Elite institutions are perceived as extracting obedience.",
    "gloss": "Distilled from raw polemical sentence: the cult of elites.",
    "targets": ["modernity", "elite_capture", "institutional_decay"],
    "tone": "severe",
    "severity": 0.72,
    "mutation_potential": 0.66,
    "doctrinal_axes": ["anti_modern", "civilization", "elite_capture"],
    "tags": ["culture_war", "elite_capture"],
}


def test_redistill_item_leaves_statement_and_gloss_untouched(tmp_path):
    cfg = make_config(tmp_path)
    response = json.dumps({"severity": 0.91, "mutation_potential": 0.44, "targets": ["obedience", "authority"]})

    with patch.object(redistill_metadata, "call_ollama", return_value=response) as mock_call:
        result = redistill_metadata.redistill_item(cfg, ITEM)

    assert result["statement"] == ITEM["statement"]
    assert result["gloss"] == ITEM["gloss"]
    assert result["severity"] == 0.91
    assert result["mutation_potential"] == 0.44
    assert result["targets"] == ["obedience", "authority"]
    # Untouched fields survive the round-trip.
    assert result["id"] == ITEM["id"]
    assert result["category"] == ITEM["category"]

    # Structured JSON output -> temperature 0 (review's real-upgrade tier §5).
    assert mock_call.call_args.kwargs["options"] == {"temperature": 0}


def test_redistill_item_clamps_out_of_range_values(tmp_path):
    cfg = make_config(tmp_path)
    response = json.dumps({"severity": 3.0, "mutation_potential": -1.0, "targets": ["x"]})

    with patch.object(redistill_metadata, "call_ollama", return_value=response):
        result = redistill_metadata.redistill_item(cfg, ITEM)

    assert result["severity"] == 1.0
    assert result["mutation_potential"] == 0.0


def test_redistill_item_keeps_original_metadata_on_llm_failure(tmp_path):
    cfg = make_config(tmp_path)

    with patch.object(redistill_metadata, "call_ollama", side_effect=RuntimeError("unreachable")):
        result = redistill_metadata.redistill_item(cfg, ITEM)

    assert result["severity"] == ITEM["severity"]
    assert result["mutation_potential"] == ITEM["mutation_potential"]
    assert result["targets"] == ITEM["targets"]


def test_redistill_item_keeps_original_targets_when_llm_omits_them(tmp_path):
    cfg = make_config(tmp_path)
    response = json.dumps({"severity": 0.8, "mutation_potential": 0.5})

    with patch.object(redistill_metadata, "call_ollama", return_value=response):
        result = redistill_metadata.redistill_item(cfg, ITEM)

    assert result["targets"] == ITEM["targets"]


def test_main_writes_a_v2_file_and_leaves_the_source_untouched(tmp_path):
    cfg = make_config(tmp_path)
    cfg.json_corpus.mkdir(parents=True)
    source = cfg.json_corpus / "culture_war.json"
    original_text = json.dumps([ITEM, {**ITEM, "id": "culture_war_0002"}])
    source.write_text(original_text, encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"paths:\n  json_corpus: {cfg.json_corpus.name}\n",
        encoding="utf-8",
    )

    response = json.dumps({"severity": 0.6, "mutation_potential": 0.3, "targets": ["a", "b"]})

    old_argv = sys.argv
    sys.argv = ["redistill_metadata.py", "--config", str(config_path)]
    try:
        with patch.object(redistill_metadata, "call_ollama", return_value=response):
            redistill_metadata.main()
    finally:
        sys.argv = old_argv

    v2_path = cfg.json_corpus / "culture_war_v2.json"
    assert v2_path.exists()

    # Source file is byte-for-byte untouched.
    assert source.read_text(encoding="utf-8") == original_text

    v2_items = json.loads(v2_path.read_text(encoding="utf-8"))
    assert len(v2_items) == 2
    assert all(item["severity"] == 0.6 for item in v2_items)
    assert all(item["statement"] == ITEM["statement"] for item in v2_items)
