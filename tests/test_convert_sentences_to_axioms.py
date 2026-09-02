from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "convert_sentences_to_axioms.py"
_spec = importlib.util.spec_from_file_location("convert_sentences_to_axioms", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
convert = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(convert)


def test_clean_line_strips_numbering_and_collapses_whitespace():
    assert convert.clean_line("  3.   The cult of   elites is cultural suicide.  ") == (
        "The cult of elites is cultural suicide."
    )


def test_classify_picks_the_theme_with_the_most_keyword_hits():
    assert convert.classify("the elite globalist banking cartel runs everything") == "elite_capture"
    assert convert.classify("nothing here matches any theme keyword") == "culture_war"


def test_main_records_source_file_and_source_line_provenance(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw_sentences"
    raw_dir.mkdir()
    out_path = tmp_path / "culture_war.json"

    (raw_dir / "sentences_a.txt").write_text(
        "too short\n"
        "The cult of political elites is cultural suicide for the whole nation.\n",
        encoding="utf-8",
    )
    (raw_dir / "sentences_b.txt").write_text(
        "Censorship always arrives disguised as protection from harm for everyone.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(convert, "RAW_DIR", raw_dir)
    monkeypatch.setattr(convert, "OUT", out_path)

    convert.main()

    entries = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(entries) == 2

    by_file = {e["source_file"]: e for e in entries}
    assert by_file["sentences_a.txt"]["source_line"] == 2  # line 1 was too short, filtered out
    assert by_file["sentences_b.txt"]["source_line"] == 1

    # statement/gloss/severity/mutation_potential are unaffected by this change.
    assert by_file["sentences_a.txt"]["category"] == "elite_capture"
    assert by_file["sentences_a.txt"]["severity"] == 0.72
    assert "cultural suicide" in by_file["sentences_a.txt"]["gloss"]


def test_main_deduplicates_by_theme_and_statement_not_by_source_line(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw_sentences"
    raw_dir.mkdir()
    out_path = tmp_path / "culture_war.json"

    # Two different raw sentences that both classify to the same theme
    # collapse to the same normalized statement -- only the first (by
    # source_file, source_line) should survive.
    (raw_dir / "sentences.txt").write_text(
        "The elite banking cartel oppresses the working class every single day.\n"
        "Globalist elite oligarchs control the entire corporate banking system.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(convert, "RAW_DIR", raw_dir)
    monkeypatch.setattr(convert, "OUT", out_path)

    convert.main()

    entries = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["source_line"] == 1
