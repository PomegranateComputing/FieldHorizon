"""
Normalization and quality.

Covers required cases 23-27 (TXT, EPUB, HTML, XML/TEI, PDF with a text
layer) and the quality scorer.

The property this file exists to defend above all others: **no blind
length threshold**. A declaration, a papal bull, a Dada manifesto, and a
sonnet are all short and all exactly what this corpus is for. A minimum
word count would delete the manifesto half of the corpus while reporting
excellent quality metrics.
"""

from __future__ import annotations

import pytest

from fieldhorizon.corpus.language import detect_language, normalize_language_code
from fieldhorizon.corpus.normalization import (
    NormalizationError,
    normalize,
    normalize_epub,
    normalize_html,
    normalize_pdf,
    normalize_txt,
    normalize_unicode,
    normalize_xml,
    rank_format,
    repair_mojibake,
    split_gutenberg_boilerplate,
    strip_repeated_lines,
)
from fieldhorizon.corpus.quality import (
    QualityThresholds,
    passes,
    score_quality,
)
from tests.corpus_helpers import build_epub, build_pdf, fixture_bytes, fixture_text

# ------------------------------------------------------------------ 23. TXT


def test_txt_normalization_separates_gutenberg_boilerplate_from_the_body():
    result = normalize_txt(fixture_bytes("gutenberg_text.txt"))

    assert result.source_format == "txt"
    # The work itself survives...
    assert "A spectre is haunting Europe" in result.text
    assert "Working men of all countries, unite!" in result.text
    # ...and the wrapper does not pollute every chunk of every Gutenberg
    # text with 3 kB of identical legal boilerplate.
    assert "START OF THE PROJECT GUTENBERG EBOOK" not in result.text
    assert "THE FULL PROJECT GUTENBERG LICENSE" not in result.text
    assert result.metrics["gutenberg_boilerplate_removed"] is True


def test_the_licence_text_is_preserved_as_evidence_not_merely_deleted():
    """
    Boilerplate stripping is only safe because the licence is captured
    first. Evidence that exists only inside a file we then edited is not
    evidence.
    """
    result = normalize_txt(fixture_bytes("gutenberg_text.txt"))

    assert result.license_text
    assert "PROJECT GUTENBERG LICENSE" in result.license_text.upper()


def test_split_gutenberg_boilerplate_returns_both_halves():
    body, boilerplate = split_gutenberg_boilerplate(fixture_text("gutenberg_text.txt"))
    assert "spectre of Communism" in body
    assert "START OF THE PROJECT GUTENBERG" in boilerplate
    assert "FULL PROJECT GUTENBERG LICENSE" in boilerplate


def test_text_with_no_gutenberg_markers_is_returned_unchanged():
    plain = "A book with no wrapper at all.\n\nSecond paragraph."
    body, boilerplate = split_gutenberg_boilerplate(plain)
    assert body == plain
    assert boilerplate == ""


def test_unicode_is_normalized_to_nfc_without_editorial_rewriting():
    # Composed and decomposed forms unify...
    assert normalize_unicode("école") == normalize_unicode("école")
    # ...but typography that is part of the published text is preserved.
    preserved = normalize_unicode("“Quoted” — em-dashed … ﬁligree")
    assert "“" in preserved and "”" in preserved
    assert "—" in preserved
    assert "…" in preserved
    # NFC, not NFKC: NFKC would split the ligature, which is an editorial
    # change dressed up as normalization.
    assert "ﬁ" in preserved


def test_line_endings_and_control_characters_are_cleaned():
    messy = "line one\r\nline two\r\tkept\x00\x07​zero-width\nline three"
    cleaned = normalize_unicode(messy)

    assert "\r" not in cleaned
    assert "\x00" not in cleaned
    assert "\x07" not in cleaned
    assert "​" not in cleaned
    assert "\t" in cleaned  # tabs are meaningful whitespace and survive
    assert "line three" in cleaned


def test_mojibake_is_repaired_only_when_the_round_trip_is_exact():
    broken = "Ã‰cole franÃ§aise de rÃ©flexion"
    repaired, was_repaired = repair_mojibake(broken)
    assert was_repaired is True
    assert "française" in repaired

    clean = "École française, correctly encoded."
    unchanged, was_repaired = repair_mojibake(clean)
    assert was_repaired is False
    assert unchanged == clean


def test_running_headers_are_stripped_but_a_repeated_refrain_is_not():
    pages: list[str] = []
    for i in range(12):
        pages.append("THE HISTORY OF ROME")  # the running header on every page
        pages.append(f"Page {i} carries its own distinct sentence of real content.")
    stripped, removed = strip_repeated_lines("\n".join(pages))

    assert "THE HISTORY OF ROME" not in stripped
    assert "distinct sentence of real content" in stripped
    assert removed == 12

    # A repeated full sentence is a refrain or a liturgical response, not
    # boilerplate, and must survive.
    refrain = "\n".join(["And the people answered, so let it be done." for _ in range(8)])
    kept, removed = strip_repeated_lines(refrain)
    assert "so let it be done" in kept
    assert removed == 0


# ----------------------------------------------------------------- 24. EPUB


def test_epub_is_read_in_spine_order_not_archive_order():
    """
    A normalizer that iterated the zip's member list would produce the
    chapters backwards here -- and a backwards book is very hard to
    notice from a quality score.
    """
    data = build_epub(reverse_spine=True)
    result = normalize_epub(data)

    assert result.source_format == "epub"
    alpha = result.text.index("alpha")
    beta = result.text.index("beta")
    gamma = result.text.index("gamma")
    assert alpha < beta < gamma


def test_epub_preserves_chapter_headings_as_structure():
    result = normalize_epub(build_epub())
    assert "Chapter One" in result.structure
    assert "Chapter Three" in result.structure
    assert result.metrics["spine_items"] == 3
    assert result.metrics["spine_items_read"] == 3


def test_an_epub_with_no_readable_spine_fails_clearly():
    from tests.corpus_helpers import build_zip

    not_an_epub = build_zip({"random.txt": b"no container.xml here"})
    with pytest.raises(NormalizationError, match="container.xml"):
        normalize_epub(not_an_epub)


def test_an_epub_bomb_member_is_refused():
    from fieldhorizon.corpus.security import UnsafeArchiveError

    with pytest.raises(UnsafeArchiveError):
        normalize_epub(build_epub(bomb_member=True))


# ----------------------------------------------------------------- 25. HTML


def test_html_normalization_keeps_content_and_drops_chrome():
    result = normalize_html(fixture_bytes("sample_page.html"))

    assert result.source_format == "html"
    assert "government of a city" in result.text
    assert "Italian republics" in result.text
    assert "Advertisement" not in result.text
    assert "attacker.example.com" not in result.text
    assert "Chapter I" in result.structure


def test_html_heading_hierarchy_is_recovered():
    result = normalize_html(fixture_bytes("sample_page.html"))
    assert result.structure[:2] == ["On the Government of Cities", "Chapter I"]


def test_html_meta_charset_overrides_a_wrong_byte_level_guess():
    html = (
        '<html><head><meta charset="latin-1"></head><body><p>'
        + "caf\xe9 na\xefve r\xe9sum\xe9 " * 30
        + "</p></body></html>"
    ).encode("latin-1")
    result = normalize_html(html)
    assert "café" in result.text
    assert "naïve" in result.text


# -------------------------------------------------------------- 26. XML/TEI


def test_tei_body_is_extracted_and_apparatus_is_dropped():
    result = normalize_xml(fixture_bytes("sample_tei.xml"))

    assert result.source_format == "tei"
    # Compared with whitespace collapsed: the extractor preserves the
    # source's own line breaks, which is correct but makes a naive
    # substring match depend on the fixture's line wrapping.
    flattened = " ".join(result.text.split())
    assert "Nature hath made men so equall" in flattened
    assert "of every man, against every man" in flattened

    # teiHeader is metadata, <note> is an editorial note, <fw> is a
    # running head, <back> is appendix matter -- all apparatus, none of
    # it the work.
    assert "Must Not Leak Into The Body" not in result.text
    assert "editorial note which is apparatus" not in result.text
    assert "RUNNING HEAD" not in result.text
    assert "appendix that TEI marks" not in result.text


def test_tei_headings_become_structure():
    result = normalize_xml(fixture_bytes("sample_tei.xml"))
    assert "Chapter the First" in result.structure
    assert "Chapter the Second" in result.structure


def test_generic_xml_falls_back_to_a_text_walk():
    generic = b"<?xml version='1.0'?><doc><p>First paragraph.</p><p>Second paragraph.</p></doc>"
    result = normalize_xml(generic)
    assert result.source_format == "xml"
    assert "First paragraph." in result.text
    assert "Second paragraph." in result.text


def test_xml_with_no_text_content_is_an_error():
    with pytest.raises(NormalizationError, match="no text content"):
        normalize_xml(b"<?xml version='1.0'?><doc><empty/></doc>")


# ------------------------------------------------------------------ 27. PDF


def test_pdf_with_a_text_layer_is_extracted():
    lines = [
        "OF THE ORIGINAL CONTRACT",
        "Man, born free, is everywhere in chains.",
        "One thinks himself the master of others,",
        "and remains a greater slave than they.",
    ]
    result = normalize_pdf(build_pdf(lines))

    assert result.source_format == "pdf"
    assert "born free" in result.text
    assert "greater slave" in result.text
    assert result.metrics["extraction_quality"] > 0.8


def test_a_pdf_with_no_text_layer_asks_for_ocr_rather_than_failing():
    """
    A scanned page is not a broken document.

    Phase I raised NormalizationError here, which the orchestrator treats
    as FINAL -- correctly, for a malformed EPUB, which will still be
    malformed next week. A scan is the opposite case: nothing is wrong
    with it, and a capability that does not exist today may exist
    tomorrow. So it raises NeedsOCR, which is deliberately NOT a
    NormalizationError, and the document rests in OCR_PENDING.

    Silently producing text of unknown provenance would still be worse
    than either.
    """
    from fieldhorizon.corpus.ocr import NEEDS_OCR_MARKER, NeedsOCR

    with pytest.raises(NeedsOCR, match="page images") as caught:
        normalize_pdf(build_pdf(["ignored"], with_text_layer=False))

    assert caught.value.marker == NEEDS_OCR_MARKER
    assert not isinstance(caught.value, NormalizationError), (
        "a scan must not be routed to the final-failure path"
    )


def test_a_non_pdf_payload_is_rejected_by_the_pdf_normalizer():
    with pytest.raises(NormalizationError, match="Not a PDF"):
        normalize_pdf(b"just some text, not a PDF at all")


def test_poor_pdf_extraction_costs_quality_points():
    report = score_quality(
        "a" * 500,
        document_type="book",
        source_format="pdf",
        format_metrics={"extraction_quality": 0.4},
    )
    assert "poor_pdf_text_layer" in report.issues


# ------------------------------------------------------------- format order


def test_format_preference_order_matches_the_documented_policy():
    assert rank_format("txt") < rank_format("epub") < rank_format("html")
    assert rank_format("html") < rank_format("pdf")
    assert rank_format("unknown-format") > rank_format("pdf")
    # Aliases resolve rather than sorting last.
    assert rank_format("text") == rank_format("txt")
    assert rank_format("htm") == rank_format("html")


def test_dispatch_routes_by_sniffed_mime():
    assert normalize(fixture_bytes("sample_book.txt"), "text/plain").source_format == "txt"
    assert normalize(fixture_bytes("sample_page.html"), "text/html").source_format == "html"
    assert normalize(build_epub(), "application/epub+zip").source_format == "epub"
    assert normalize(build_pdf(["hello world"]), "application/pdf").source_format == "pdf"

    with pytest.raises(NormalizationError, match="No normalizer"):
        normalize(b"x", "application/octet-stream")


# ------------------------------------------------------------------ quality


def test_a_short_manifesto_is_not_penalized_for_being_short():
    """
    The single most important quality behaviour. This document is ~1.3 kB
    -- a blind long-form threshold would reject it outright.
    """
    text = fixture_text("sample_manifesto.txt")
    assert len(text) < 2000

    report = score_quality(text, declared_language="en", document_type="declaration")
    assert passes(report)
    assert "short_for_declared_form" not in report.issues


def test_a_short_text_claiming_to_be_a_novel_is_penalized():
    report = score_quality(
        "A very short piece of prose. " * 10, declared_language="en", document_type="novel"
    )
    assert "short_for_declared_form" in report.issues


def test_a_full_length_book_scores_well():
    # Every line distinct. A text pasted together from twelve copies of
    # the same chapter genuinely IS a repetitive document, and the
    # repetition detector is right to flag it -- so a "healthy long book"
    # fixture has to actually vary.
    text = fixture_text("sample_book.txt") + "\n\n" + "\n\n".join(
        f"CHAPTER {n}\n\n"
        f"In the {n}th season of that survey I recorded {n * 7} nests upon the "
        f"eastern face, a number which exceeded by {n * 3} the count of the "
        f"preceding year, and which the fishermen of the parish attributed to "
        f"the mildness of the {1840 + n} winter rather than to any change in "
        f"the birds themselves. Lindqvist, writing of the same district some "
        f"{n * 11} years earlier, gives figures that cannot easily be "
        f"reconciled with mine, and I set them down here without attempting "
        f"to resolve the discrepancy."
        for n in range(1, 40)
    )
    report = score_quality(text, declared_language="en", document_type="book")

    assert passes(report)
    assert report.score > 0.7
    assert report.language_detected == "en"
    assert "high_line_repetition" not in report.issues


def test_mojibake_and_replacement_characters_cost_points():
    good = score_quality("Clean readable English prose about many things. " * 60, document_type="essay")
    bad = score_quality(
        ("Clean readable English prose about many things. " * 55)
        + ("Ã© Ã¨ â€™ ï¿½ " * 40),
        document_type="essay",
    )
    assert bad.score < good.score
    assert "mojibake_density" in bad.issues


def test_empty_or_negligible_text_scores_zero():
    report = score_quality("tiny", document_type="declaration")
    assert report.score == 0.0
    assert "empty_or_negligible_text" in report.issues
    assert not passes(report)


def test_ocr_fragmentation_is_detected():
    fragmented = "\n".join(["a b" for _ in range(200)])
    report = score_quality(fragmented, document_type="book")
    assert "ocr_fragmentation" in report.issues or "ocr_character_scatter" in report.issues
    assert not passes(report)


def test_high_line_repetition_is_detected():
    repeated = "\n".join(["The same long line of text repeated over and over again." for _ in range(60)])
    report = score_quality(repeated, document_type="book")
    assert "high_line_repetition" in report.issues


def test_a_language_mismatch_is_recorded_but_not_fatal_by_default():
    french = fixture_text("sample_manifesto.txt")
    report = score_quality(french, declared_language="de", document_type="declaration")

    assert "language_mismatch" in report.issues
    # Catalogues mislabel constantly; the DETECTED language is stored and
    # the document is not thrown away for it.
    assert passes(report)

    strict = QualityThresholds(reject_on_language_mismatch=True)
    strict_report = score_quality(french, declared_language="de", document_type="declaration",
                                  thresholds=strict)
    assert not passes(strict_report, strict)


# ---------------------------------------------------------------- language


def test_language_detection_covers_the_priority_languages():
    samples = {
        "fr": "Le sujet de cette étude est la nature du pouvoir dans les sociétés modernes "
              "et la manière dont il se reproduit dans les institutions de la République.",
        "en": "The subject of this study is the nature of power in modern societies and the "
              "manner in which it reproduces itself within the institutions of the state.",
        "de": "Der Gegenstand dieser Untersuchung ist die Natur der Macht in den modernen "
              "Gesellschaften und die Art und Weise, wie sie sich in den Institutionen erneuert.",
        "es": "El sujeto de este estudio es la naturaleza del poder en las sociedades modernas "
              "y la manera en que se reproduce dentro de las instituciones del estado.",
        "it": "Il soggetto di questo studio è la natura del potere nelle società moderne e il "
              "modo in cui si riproduce nelle istituzioni dello stato moderno.",
        "la": "Quod in hoc libro tractatur est natura potestatis in civitatibus, et quo modo "
              "per instituta hominum atque leges se ipsam renovat atque conservat in saecula.",
    }
    for expected, text in samples.items():
        detected, confidence = detect_language(text)
        assert detected == expected, f"{expected}: got {detected}"
        assert confidence > 0.3


def test_language_detection_is_deterministic():
    text = fixture_text("sample_book.txt")
    assert detect_language(text) == detect_language(text) == detect_language(text)


def test_too_short_or_unrecognizable_text_returns_unknown():
    assert detect_language("Too short.")[0] == "unknown"
    assert detect_language(" ".join(["Zyx"] * 40))[0] == "unknown"


def test_language_codes_normalize_from_every_form_catalogues_emit():
    assert normalize_language_code("eng") == "en"
    assert normalize_language_code("fre") == "fr"
    assert normalize_language_code("fra") == "fr"
    assert normalize_language_code("en-GB") == "en"
    assert normalize_language_code("EN") == "en"
    assert normalize_language_code("http://id.loc.gov/vocabulary/iso639-2/ger") == "de"
    assert normalize_language_code("") == ""
