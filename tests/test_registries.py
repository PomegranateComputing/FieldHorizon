from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.registries import (
    EmbeddingVersionMismatch,
    _model_id_cache,
    assert_same_embedding_version,
    get_or_register_embedding_model,
    get_or_register_model,
    get_or_register_prompt,
    list_registry,
    show_registry_entry,
)


@pytest.fixture(autouse=True)
def _clear_model_id_cache():
    # _model_id_cache is process-global (same shape/reason as
    # embeddings._embedding_version_cache); tests reusing the same
    # (base_url, model_name) pair would otherwise leak a cached
    # resolution across test functions.
    _model_id_cache.clear()
    yield
    _model_id_cache.clear()


def make_config(tmp_path: Path) -> AppConfig:
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


def test_assert_same_embedding_version_passes_for_matching_versions():
    assert_same_embedding_version("nomic-embed-text@abc", "nomic-embed-text@abc")


def test_assert_same_embedding_version_raises_for_mismatched_versions():
    with pytest.raises(EmbeddingVersionMismatch, match="never comparable"):
        assert_same_embedding_version("nomic-embed-text@abc", "nomic-embed-text@def")


def test_get_or_register_model_registers_on_first_sight(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with patch("fieldhorizon.registries.requests.get", side_effect=RuntimeError("no ollama")):
        model_id = get_or_register_model(cfg, "hermes3:8b")

    assert model_id == "hermes3:8b"  # falls back to bare name when digest resolution fails
    with connect(cfg.database) as conn:
        row = conn.execute("SELECT model_id, name FROM model_registry WHERE model_id = ?", (model_id,)).fetchone()
    assert row is not None
    assert row["name"] == "hermes3:8b"


def test_get_or_register_model_is_idempotent(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with patch("fieldhorizon.registries.requests.get", side_effect=RuntimeError("no ollama")):
        get_or_register_model(cfg, "hermes3:8b")
        get_or_register_model(cfg, "hermes3:8b")

    with connect(cfg.database) as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM model_registry").fetchone()["n"]
    assert count == 1


def test_get_or_register_model_uses_digest_when_resolvable(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "hermes3:8b", "digest": "abcdef1234567890"}]}

    with patch("fieldhorizon.registries.requests.get", return_value=FakeResponse()):
        model_id = get_or_register_model(cfg, "hermes3:8b")

    assert model_id == "hermes3:8b@abcdef123456"


def test_get_or_register_prompt_registers_by_template_hash(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    prompt_id_1 = get_or_register_prompt(cfg, "critic", "template text A")
    prompt_id_2 = get_or_register_prompt(cfg, "critic", "template text A")
    prompt_id_3 = get_or_register_prompt(cfg, "critic", "template text B")

    assert prompt_id_1 == prompt_id_2
    assert prompt_id_1 != prompt_id_3

    with connect(cfg.database) as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM prompt_registry").fetchone()["n"]
    assert count == 2


def test_get_or_register_embedding_model_registers_by_version_string(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    embedding_model_id = get_or_register_embedding_model(cfg, "nomic-embed-text@abc123", "nomic-embed-text", dim=768)

    assert embedding_model_id == "nomic-embed-text@abc123"
    with connect(cfg.database) as conn:
        row = conn.execute(
            "SELECT model_name, dim FROM embedding_registry WHERE embedding_model_id = ?", (embedding_model_id,)
        ).fetchone()
    assert row["model_name"] == "nomic-embed-text"
    assert row["dim"] == 768


def test_list_registry_returns_empty_before_anything_is_registered(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert list_registry(cfg, "model") == []


def test_list_registry_and_show_registry_entry_round_trip(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    with patch("fieldhorizon.registries.requests.get", side_effect=RuntimeError("no ollama")):
        model_id = get_or_register_model(cfg, "hermes3:8b")

    entries = list_registry(cfg, "model")
    assert len(entries) == 1
    assert entries[0]["id"] == model_id
    assert entries[0]["name"] == "hermes3:8b"

    entry = show_registry_entry(cfg, "model", model_id)
    assert entry is not None
    assert entry["model_id"] == model_id
    assert entry["active"] == 1


def test_show_registry_entry_returns_none_for_unknown_id(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert show_registry_entry(cfg, "model", "no-such-model") is None


def test_list_registry_rejects_an_unknown_kind(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with pytest.raises(ValueError, match="Unknown registry kind"):
        list_registry(cfg, "bogus")
