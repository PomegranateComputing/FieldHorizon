from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from fieldhorizon.config import AppConfig
from fieldhorizon.llm import OllamaError, call_ollama, embed


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


def make_response(content: str, status_code: int = 200) -> MagicMock:
    res = MagicMock()
    res.status_code = status_code
    res.json.return_value = {"message": {"content": content}}

    if status_code >= 400:
        res.raise_for_status.side_effect = requests.exceptions.HTTPError(response=res)
    else:
        res.raise_for_status.side_effect = None

    return res


def make_embedding_response(vector: list[float], status_code: int = 200) -> MagicMock:
    res = MagicMock()
    res.status_code = status_code
    res.json.return_value = {"embedding": vector}

    if status_code >= 400:
        res.raise_for_status.side_effect = requests.exceptions.HTTPError(response=res)
    else:
        res.raise_for_status.side_effect = None

    return res


def test_empty_content_raises_instead_of_returning_empty_string(tmp_path):
    cfg = make_config(tmp_path)

    with patch("fieldhorizon.llm._session.post", return_value=make_response("")), pytest.raises(OllamaError):
        call_ollama(cfg, "prompt")


def test_successful_response_returns_stripped_content(tmp_path):
    cfg = make_config(tmp_path)

    with patch("fieldhorizon.llm._session.post", return_value=make_response("  a fragment  ")):
        result = call_ollama(cfg, "prompt")

    assert result == "a fragment"


def test_connection_error_is_retried_then_succeeds(tmp_path):
    cfg = make_config(tmp_path)

    with patch(
        "fieldhorizon.llm._session.post",
        side_effect=[
            requests.exceptions.ConnectionError("refused"),
            make_response("a fragment after retry"),
        ],
    ), patch("fieldhorizon.llm.time.sleep"):
        result = call_ollama(cfg, "prompt")

    assert result == "a fragment after retry"


def test_connection_error_exhausts_retries_and_raises(tmp_path):
    cfg = make_config(tmp_path)

    with patch(
        "fieldhorizon.llm._session.post",
        side_effect=requests.exceptions.ConnectionError("refused"),
    ) as mock_post, patch("fieldhorizon.llm.time.sleep"), pytest.raises(OllamaError):
        call_ollama(cfg, "prompt")

    assert mock_post.call_count == 3


def test_client_error_is_not_retried(tmp_path):
    cfg = make_config(tmp_path)

    with patch(
        "fieldhorizon.llm._session.post",
        return_value=make_response("", status_code=404),
    ) as mock_post, patch("fieldhorizon.llm.time.sleep"), pytest.raises(OllamaError):
        call_ollama(cfg, "prompt")

    assert mock_post.call_count == 1


def test_call_ollama_options_override_configured_defaults(tmp_path):
    cfg = make_config(tmp_path)

    with patch(
        "fieldhorizon.llm._session.post", return_value=make_response("structured")
    ) as mock_post:
        call_ollama(cfg, "prompt", options={"temperature": 0})

    sent_options = mock_post.call_args.kwargs["json"]["options"]
    assert sent_options["temperature"] == 0
    assert sent_options["top_p"] == cfg.top_p


def test_embed_returns_vector_from_embeddings_endpoint(tmp_path):
    cfg = make_config(tmp_path)

    with patch(
        "fieldhorizon.llm._session.post",
        return_value=make_embedding_response([0.1, 0.2, 0.3]),
    ) as mock_post:
        vector = embed(cfg, "a fragment of doctrine")

    assert vector == [0.1, 0.2, 0.3]
    assert mock_post.call_args.args[0].endswith("/api/embeddings")
    assert mock_post.call_args.kwargs["json"]["model"] == "nomic-embed-text"


def test_embed_raises_on_empty_embedding(tmp_path):
    cfg = make_config(tmp_path)

    with patch(
        "fieldhorizon.llm._session.post",
        return_value=make_embedding_response([]),
    ), pytest.raises(OllamaError):
        embed(cfg, "a fragment of doctrine")
