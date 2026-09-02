from __future__ import annotations

from unittest.mock import patch

from fieldhorizon.config import AppConfig
from fieldhorizon.interpreter import interpret_query


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


def test_interpret_query_calls_ollama_at_temperature_zero(tmp_path):
    cfg = make_config(tmp_path)

    with patch(
        "fieldhorizon.interpreter.call_ollama",
        return_value='{"retrieval_query": "expanded query"}',
    ) as mock_call:
        interpret_query(cfg, "a raw query")

    assert mock_call.call_args.kwargs["options"] == {"temperature": 0}


def test_interpret_query_falls_back_on_ollama_failure(tmp_path):
    cfg = make_config(tmp_path)

    with patch("fieldhorizon.interpreter.call_ollama", side_effect=RuntimeError("unreachable")):
        data = interpret_query(cfg, "a raw query")

    assert data["retrieval_query"] == "a raw query"
    assert data["notes"] == ["fallback_interpretation_used"]
