from __future__ import annotations

from unittest.mock import MagicMock, patch

from fieldhorizon.config import AppConfig
from fieldhorizon.embeddings import _embedding_version_cache, current_embedding_version


def make_config(tmp_path, model: str = "nomic-embed-text") -> AppConfig:
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
        embedding_model=model,
    )


def make_tags_response(models: list[dict]) -> MagicMock:
    res = MagicMock()
    res.raise_for_status.side_effect = None
    res.json.return_value = {"models": models}
    return res


def test_current_embedding_version_includes_digest_when_resolvable(tmp_path):
    cfg = make_config(tmp_path)
    _embedding_version_cache.clear()

    tags = make_tags_response(
        [{"name": "nomic-embed-text:latest", "digest": "abcdef0123456789"}]
    )

    with patch("fieldhorizon.embeddings.requests.get", return_value=tags):
        version = current_embedding_version(cfg)

    assert version == "nomic-embed-text@abcdef012345"


def test_current_embedding_version_falls_back_to_bare_model_name_on_failure(tmp_path):
    cfg = make_config(tmp_path)
    _embedding_version_cache.clear()

    with patch("fieldhorizon.embeddings.requests.get", side_effect=RuntimeError("unreachable")):
        version = current_embedding_version(cfg)

    assert version == "nomic-embed-text"


def test_current_embedding_version_falls_back_when_model_not_listed(tmp_path):
    cfg = make_config(tmp_path)
    _embedding_version_cache.clear()

    tags = make_tags_response([{"name": "some-other-model:latest", "digest": "zzz"}])

    with patch("fieldhorizon.embeddings.requests.get", return_value=tags):
        version = current_embedding_version(cfg)

    assert version == "nomic-embed-text"


def test_current_embedding_version_is_cached_per_process(tmp_path):
    cfg = make_config(tmp_path)
    _embedding_version_cache.clear()

    tags = make_tags_response(
        [{"name": "nomic-embed-text:latest", "digest": "abcdef0123456789"}]
    )

    with patch("fieldhorizon.embeddings.requests.get", return_value=tags) as mock_get:
        first = current_embedding_version(cfg)
        second = current_embedding_version(cfg)

    assert first == second
    mock_get.assert_called_once()


def test_current_embedding_version_matches_bare_name_without_tag_suffix(tmp_path):
    cfg = make_config(tmp_path)
    _embedding_version_cache.clear()

    # Ollama lists models with a ":tag" suffix even when the config only
    # names the bare model.
    tags = make_tags_response([{"name": "nomic-embed-text", "digest": "111222333444"}])

    with patch("fieldhorizon.embeddings.requests.get", return_value=tags):
        version = current_embedding_version(cfg)

    assert version == "nomic-embed-text@111222333444"
