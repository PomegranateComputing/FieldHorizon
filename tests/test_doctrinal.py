from __future__ import annotations

from unittest.mock import patch

import pytest

from fieldhorizon import doctrinal
from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.doctrinal import (
    get_axiom_vector,
    score_doctrinal_enforcement,
    score_doctrinal_enforcement_embedding,
    score_doctrinal_enforcement_keyword,
    score_symbolic_density,
    score_symbolic_density_embedding,
    score_symbolic_density_keyword,
    split_sentences,
)


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


def insert_json_entry(cfg: AppConfig, entry_id: str, category: str, statement: str) -> None:
    with connect(cfg.database) as conn:
        conn.execute(
            """
            INSERT INTO json_entries(id, group_name, category, statement, raw_json)
            VALUES (?, 'test_group', ?, ?, '{}')
            """,
            (entry_id, category, statement),
        )
        conn.commit()


def test_split_sentences_splits_on_terminal_punctuation():
    text = "The machine judges. It does not forgive! Does it ever relent?"
    assert split_sentences(text) == [
        "The machine judges.",
        "It does not forgive!",
        "Does it ever relent?",
    ]


def test_score_doctrinal_enforcement_falls_back_to_keyword_without_cfg():
    response = "FIELD_FRAGMENT\nThe tawhid of the machine is a false unity.\n\nMETADATA_JSON\n{}"
    json_rows = [{"category": "tawhid", "statement": "Unity admits no rival."}]

    score, method = score_doctrinal_enforcement(None, response, json_rows)

    assert method == "keyword"
    assert score == score_doctrinal_enforcement_keyword(response, json_rows)


def test_score_doctrinal_enforcement_falls_back_when_embedding_call_fails(tmp_path):
    cfg = make_config(tmp_path)
    response = "FIELD_FRAGMENT\nThe tawhid of the machine is a false unity.\n\nMETADATA_JSON\n{}"
    json_rows = [{"category": "tawhid", "statement": "Unity admits no rival."}]

    with patch("fieldhorizon.doctrinal.embed_text", side_effect=RuntimeError("no embedding model")):
        score, method = score_doctrinal_enforcement(cfg, response, json_rows)

    assert method == "keyword"
    assert score == score_doctrinal_enforcement_keyword(response, json_rows)


def test_score_doctrinal_enforcement_uses_embedding_when_available(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    insert_json_entry(cfg, "ax1", "tawhid", "The machine is a false idol.")

    response = "FIELD_FRAGMENT\nThe machine is a false idol of unity.\n\nMETADATA_JSON\n{}"
    json_rows = [{"id": "ax1", "category": "tawhid", "statement": "The machine is a false idol."}]

    # Sentence and axiom-statement vectors are identical here -> perfect
    # cosine similarity of 1.0 regardless of exact text, isolating the
    # code path (embedding selected, mean-over-axioms computed) from the
    # embedding model's actual semantics.
    with patch("fieldhorizon.doctrinal.embed_text", return_value=[1.0, 0.0]):
        score, method = score_doctrinal_enforcement(cfg, response, json_rows)

    assert method == "embedding"
    assert score == 1.0


def test_get_axiom_vector_is_cached_after_first_embed(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    insert_json_entry(cfg, "ax1", "tawhid", "the machine is a false idol")

    with patch("fieldhorizon.doctrinal.embed_text", return_value=[0.1, 0.2]) as mock_embed:
        first = get_axiom_vector(cfg, "ax1", "the machine is a false idol")
        second = get_axiom_vector(cfg, "ax1", "the machine is a false idol")

    assert first == pytest.approx([0.1, 0.2], abs=1e-6)
    assert second == pytest.approx([0.1, 0.2], abs=1e-6)
    mock_embed.assert_called_once()


def test_score_doctrinal_enforcement_embedding_raises_with_no_sentences(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    response = "FIELD_FRAGMENT\n\n\nMETADATA_JSON\n{}"
    json_rows = [{"id": "ax1", "category": "tawhid", "statement": "Unity admits no rival."}]

    with patch("fieldhorizon.doctrinal.embed_text", return_value=[1.0, 0.0]):
        try:
            score_doctrinal_enforcement_embedding(cfg, response, json_rows)
            raised = False
        except ValueError:
            raised = True

    assert raised


def test_score_symbolic_density_falls_back_to_keyword_without_cfg():
    response = "FIELD_FRAGMENT\nThe machine judges without mercy or audit.\n\nMETADATA_JSON\n{}"

    score, method = score_symbolic_density(None, response)

    assert method == "keyword"
    assert score == score_symbolic_density_keyword(response)


def test_score_symbolic_density_falls_back_when_embedding_call_fails(tmp_path):
    cfg = make_config(tmp_path)
    response = "FIELD_FRAGMENT\nThe machine judges without mercy or audit.\n\nMETADATA_JSON\n{}"

    with patch("fieldhorizon.doctrinal.embed_text", side_effect=RuntimeError("no embedding model")):
        score, method = score_symbolic_density(cfg, response)

    assert method == "keyword"
    assert score == score_symbolic_density_keyword(response)


def test_score_symbolic_density_embedding_counts_sentences_over_threshold(tmp_path):
    cfg = make_config(tmp_path)
    # Two sentences: one embedded identically to the "judgment" term
    # vector (a hit), one orthogonal to every term vector (a miss) --
    # isolates the counting/threshold logic from the embedding model's
    # actual semantics. All 21 SYMBOLIC_TERMS get embedded too (and
    # cached), so every term other than "judgment" is pinned to its own
    # shared axis, distinct from both sentences.
    response = "FIELD_FRAGMENT\nOn judgment. On something else entirely.\n\nMETADATA_JSON\n{}"

    def fake_embed(cfg, text, model=None):
        if text in ("judgment", "On judgment."):
            return [1.0, 0.0, 0.0]
        if text == "On something else entirely.":
            return [0.0, 0.0, 1.0]
        return [0.0, 1.0, 0.0]  # the other 20 symbolic terms

    # The term-vector cache is process-global and keyed by (model, term);
    # clear it so this test's fake_embed is what actually populates it,
    # regardless of what ran earlier in the suite.
    doctrinal._symbolic_term_vector_cache.clear()

    with patch("fieldhorizon.doctrinal.embed_text", side_effect=fake_embed):
        score = score_symbolic_density_embedding(cfg, response)

    assert score == pytest.approx(0.5)


def test_score_symbolic_density_embedding_raises_with_no_sentences(tmp_path):
    cfg = make_config(tmp_path)
    response = "FIELD_FRAGMENT\n\n\nMETADATA_JSON\n{}"

    with patch("fieldhorizon.doctrinal.embed_text", return_value=[1.0, 0.0]):
        try:
            score_symbolic_density_embedding(cfg, response)
            raised = False
        except ValueError:
            raised = True

    assert raised
