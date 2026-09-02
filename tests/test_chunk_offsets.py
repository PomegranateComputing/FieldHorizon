from __future__ import annotations

from pathlib import Path

from fieldhorizon.config import AppConfig
from fieldhorizon.db import connect, init_db
from fieldhorizon.ingest import backfill_chunk_offsets, chunk_text, ingest_books


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


def test_chunk_text_offsets_resolve_to_the_actual_source_text():
    text = "  leading whitespace then a real sentence that keeps going for a while here.  \n\ntrailing chunk content after the overlap boundary continues onward."

    chunks = chunk_text(text, size=60, overlap=10)

    assert len(chunks) > 1
    for content, char_start, char_end in chunks:
        assert text[char_start:char_end] == content


def test_chunk_text_strips_leading_whitespace_out_of_the_offset():
    text = "   \n\n   the actual content starts here and this is long enough to matter for the test"
    chunks = chunk_text(text, size=1000, overlap=0)

    assert len(chunks) == 1
    content, char_start, char_end = chunks[0]
    assert content == text.strip()
    assert text[char_start:char_end] == content
    assert char_start > 0  # leading whitespace was excluded from the range


def test_ingest_books_stores_offsets_that_resolve_to_the_source_file(tmp_path):
    cfg = make_config(tmp_path)
    cfg.books.mkdir(parents=True, exist_ok=True)
    init_db(cfg.database)

    source_text = "The machine is an idol of judgment and the faithful bow before its unblinking audit."
    (cfg.books / "sample.txt").write_text(source_text, encoding="utf-8")

    ingest_books(cfg)

    with connect(cfg.database) as conn:
        row = conn.execute("SELECT content, char_start, char_end FROM chunks").fetchone()

    assert row["char_start"] is not None
    assert row["char_end"] is not None
    assert source_text[row["char_start"] : row["char_end"]] == row["content"]


def test_backfill_chunk_offsets_resolves_missing_offsets_from_the_source_file(tmp_path):
    cfg = make_config(tmp_path)
    cfg.books.mkdir(parents=True, exist_ok=True)
    init_db(cfg.database)

    source_path = cfg.books / "sample.txt"
    source_text = "The machine is an idol of judgment and the faithful bow before its unblinking audit."
    source_path.write_text(source_text, encoding="utf-8")

    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO sources(title, path, source_type) VALUES ('sample', ?, 'book')",
            (str(source_path),),
        )
        source_id = conn.execute("SELECT id FROM sources WHERE title = 'sample'").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate)
            VALUES (?, 0, 'sample / chunk 00000', ?, 10)
            """,
            (source_id, source_text),
        )
        conn.commit()

    found, missed = backfill_chunk_offsets(cfg)

    assert found == 1
    assert missed == 0

    with connect(cfg.database) as conn:
        row = conn.execute("SELECT char_start, char_end FROM chunks").fetchone()

    assert source_text[row["char_start"] : row["char_end"]] == source_text


def test_backfill_chunk_offsets_logs_and_counts_a_miss_when_content_not_found(tmp_path):
    cfg = make_config(tmp_path)
    cfg.books.mkdir(parents=True, exist_ok=True)
    init_db(cfg.database)

    source_path = cfg.books / "sample.txt"
    source_path.write_text("completely different content now", encoding="utf-8")

    with connect(cfg.database) as conn:
        conn.execute(
            "INSERT INTO sources(title, path, source_type) VALUES ('sample', ?, 'book')",
            (str(source_path),),
        )
        source_id = conn.execute("SELECT id FROM sources WHERE title = 'sample'").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO chunks(source_id, chunk_index, canonical_ref, content, token_estimate)
            VALUES (?, 0, 'sample / chunk 00000', 'text that was never actually there', 10)
            """,
            (source_id,),
        )
        conn.commit()

    found, missed = backfill_chunk_offsets(cfg)

    assert found == 0
    assert missed == 1


def test_backfill_chunk_offsets_skips_chunks_that_already_have_offsets(tmp_path):
    cfg = make_config(tmp_path)
    cfg.books.mkdir(parents=True, exist_ok=True)
    init_db(cfg.database)

    source_text = "The machine is an idol of judgment."
    (cfg.books / "sample.txt").write_text(source_text, encoding="utf-8")
    ingest_books(cfg)

    found, missed = backfill_chunk_offsets(cfg)

    assert found == 0
    assert missed == 0
