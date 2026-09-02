from __future__ import annotations

import sqlite3
from pathlib import Path

from fieldhorizon.canon import load_canon_fragments
from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.indexer import build_cycle_index


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


def insert_cycle(
    cfg: AppConfig,
    query: str,
    verdict: str,
    final_score: float,
    fragment: str,
    dry_run: int = 0,
) -> int:
    with connect(cfg.database) as conn:
        cur = conn.execute(
            """
            INSERT INTO cycles(query, model, prompt, response, dry_run, verdict, final_score, fragment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (query, "test-model", "prompt text", "response text", dry_run, verdict, final_score, fragment),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)


def test_connect_sets_a_nonzero_busy_timeout(tmp_path):
    """Phase UI-6 item 4: SQLite's own default busy_timeout is 0 -- a second connection hitting a writer mid-transaction fails instantly instead of waiting a moment, unrealistic for this single-machine app's own CLI-writes-while-server-reads case."""
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    with connect(cfg.database) as conn:
        (timeout_ms,) = conn.execute("PRAGMA busy_timeout").fetchone()
    assert timeout_ms > 0


def test_init_db_migrates_a_pre_existing_cycles_table_without_the_new_columns(tmp_path):
    cfg = make_config(tmp_path)
    cfg.database.parent.mkdir(parents=True, exist_ok=True)

    # Simulate a database created before the verdict/final_score/fragment
    # columns existed.
    with sqlite3.connect(cfg.database) as conn:
        conn.execute(
            """
            CREATE TABLE cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt TEXT NOT NULL,
                response TEXT NOT NULL,
                dry_run INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO cycles(query, model, prompt, response) VALUES (?, ?, ?, ?)",
            ("pre-migration query", "old-model", "old prompt", "old response"),
        )
        conn.commit()

    init_db(cfg.database)

    with connect(cfg.database) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(cycles)").fetchall()}
        assert {"verdict", "final_score", "fragment"} <= columns

        row = conn.execute("SELECT query, verdict, fragment FROM cycles WHERE query = ?", ("pre-migration query",)).fetchone()
        assert row["query"] == "pre-migration query"
        assert row["verdict"] is None
        assert row["fragment"] is None

    # Migration must be idempotent -- running it again must not raise.
    init_db(cfg.database)


def test_load_canon_fragments_reads_from_cycles_table(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    insert_cycle(cfg, "query heresy", "HERESY", 0.55, "a heretical fragment")
    insert_cycle(cfg, "query canon 1", "CANON", 0.9, "the first canon fragment")
    insert_cycle(cfg, "query canon 2", "CANON", 0.95, "the second canon fragment")
    insert_cycle(cfg, "query dry run canon", "CANON", 0.99, "should not appear", dry_run=1)

    fragments = load_canon_fragments(cfg, limit=3)

    assert len(fragments) == 2
    contents = {f["content"] for f in fragments}
    assert contents == {"the first canon fragment", "the second canon fragment"}
    assert all(f["ref"].startswith("canon / cycle_") for f in fragments)


def test_load_canon_fragments_respects_limit(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    for i in range(5):
        insert_cycle(cfg, f"query {i}", "CANON", 0.9, f"fragment {i}")

    fragments = load_canon_fragments(cfg, limit=2)
    assert len(fragments) == 2


def test_build_cycle_index_reports_canon_and_heresy_rows(tmp_path):
    cfg = make_config(tmp_path)
    cfg.outputs.mkdir(parents=True, exist_ok=True)
    init_db(cfg.database)

    insert_cycle(cfg, "the machine as idol", "CANON", 0.9, "a canon fragment")
    insert_cycle(cfg, "a failed attempt", "HERESY", 0.55, "a heresy fragment")
    insert_cycle(cfg, "a dry run", "CANON", 0.99, "should be excluded", dry_run=1)

    index_path = build_cycle_index(cfg)
    text = index_path.read_text(encoding="utf-8")

    assert "the machine as idol" in text
    assert "CANON" in text
    assert "a failed attempt" in text
    assert "HERESY" in text
    assert "should be excluded" not in text
