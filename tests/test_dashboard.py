from __future__ import annotations

from pathlib import Path

from fieldhorizon.config import AppConfig
from fieldhorizon.dashboard import render_sparkline, render_weather_html, write_weather_html
from fieldhorizon.db import init_db


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


def test_render_sparkline_empty():
    assert render_sparkline([]) == "(no history)"


def test_render_sparkline_single_value():
    assert len(render_sparkline([0.5])) == 1


def test_render_sparkline_flat_series_uses_one_repeated_char():
    result = render_sparkline([0.5, 0.5, 0.5])
    assert len(result) == 3
    assert len(set(result)) == 1


def test_render_sparkline_varies_with_value_range():
    result = render_sparkline([0.0, 0.5, 1.0])
    assert len(result) == 3
    assert result[0] != result[2]


def test_render_weather_html_on_an_empty_database(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    html_text = render_weather_html(cfg)
    assert "<html>" in html_text
    assert "Field Horizon" in html_text


def test_write_weather_html_creates_the_file(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    path = write_weather_html(cfg)
    assert path.exists()
    assert path == cfg.outputs / "weather.html"
