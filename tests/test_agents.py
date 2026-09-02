from __future__ import annotations

from unittest.mock import patch

from fieldhorizon.agents import AGENTS, AgentOutput, run_agents
from fieldhorizon.config import AppConfig


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


def test_run_agents_returns_one_output_per_agent(tmp_path):
    cfg = make_config(tmp_path)

    with patch("fieldhorizon.agents.call_ollama", return_value="a memorandum"):
        outputs = run_agents(cfg, "base prompt")

    assert [o.name for o in outputs] == [a["name"] for a in AGENTS]
    assert all(o.content == "a memorandum" for o in outputs)


def test_run_agents_calls_on_agent_done_once_per_agent_in_order(tmp_path):
    cfg = make_config(tmp_path)
    seen: list[AgentOutput] = []

    with patch("fieldhorizon.agents.call_ollama", return_value="a memorandum"):
        run_agents(cfg, "base prompt", on_agent_done=seen.append)

    assert [o.name for o in seen] == [a["name"] for a in AGENTS]
    assert len(seen) == len(AGENTS)


def test_run_agents_invokes_callback_even_when_an_agent_fails(tmp_path):
    cfg = make_config(tmp_path)
    seen: list[AgentOutput] = []

    with patch("fieldhorizon.agents.call_ollama", side_effect=RuntimeError("unreachable")):
        run_agents(cfg, "base prompt", on_agent_done=seen.append)

    assert len(seen) == len(AGENTS)
    assert all(o.content == "" for o in seen)


def test_run_agents_without_callback_does_not_raise(tmp_path):
    cfg = make_config(tmp_path)

    with patch("fieldhorizon.agents.call_ollama", return_value="a memorandum"):
        outputs = run_agents(cfg, "base prompt", on_agent_done=None)

    assert len(outputs) == len(AGENTS)
