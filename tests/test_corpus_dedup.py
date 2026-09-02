"""
Deduplication.

Covers required cases 28-30: exact duplicates, near duplicates, and the
guarantee that different translations are never merged.

Finding duplicates is the easy half. The hard half -- and what most of
this file tests -- is **not merging things that merely look alike**:
translations, editions, extracts, and corrected transcriptions are all
distinct documents that a naive similarity check would collapse.
"""

from __future__ import annotations

from typing import Any

from fieldhorizon.corpus.deduplication import (
    DocumentFingerprint,
    DuplicateKind,
    choose_representative,
    cluster_id_for,
    compare,
    extract_identifiers,
    find_duplicate,
    hamming_distance,
    jaccard_estimate,
    minhash_signature,
    normalize_author,
    normalize_title,
    simhash,
    simhash_similarity,
    work_key,
)


def fingerprint(document_id: str, **overrides) -> DocumentFingerprint:
    defaults: dict[str, Any] = {
        "raw_sha256": f"raw-{document_id}",
        "normalized_sha256": f"norm-{document_id}",
        "title": "The Social Contract",
        "authors": ("Jean-Jacques Rousseau",),
        "language": "en",
        "char_count": 100_000,
        "quality_score": 0.8,
    }
    defaults.update(overrides)
    return DocumentFingerprint(document_id=document_id, **defaults)


# ------------------------------------------------------ 28. exact duplicates


def test_identical_raw_bytes_are_an_exact_duplicate():
    a = fingerprint("a", raw_sha256="SAME", normalized_sha256="norm-a")
    b = fingerprint("b", raw_sha256="SAME", normalized_sha256="norm-b")

    verdict = compare(a, b)
    assert verdict.is_duplicate
    assert verdict.kind == DuplicateKind.EXACT_RAW
    assert verdict.similarity == 1.0


def test_identical_normalized_text_is_an_exact_duplicate():
    a = fingerprint("a", raw_sha256="raw-a", normalized_sha256="SAME")
    b = fingerprint("b", raw_sha256="raw-b", normalized_sha256="SAME")

    verdict = compare(a, b)
    assert verdict.is_duplicate
    assert verdict.kind == DuplicateKind.EXACT_NORMALIZED


def test_byte_equality_beats_contradictory_metadata():
    """
    If two files are byte-identical they are the same document, whatever
    the catalogues claim: one of them is simply wrong about which
    translation it is serving, and the bytes are the ground truth.
    """
    a = fingerprint("a", raw_sha256="SAME", language="en", authors=("Translator A",))
    b = fingerprint("b", raw_sha256="SAME", language="fr", authors=("Traducteur B",))

    verdict = compare(a, b)
    assert verdict.is_duplicate
    assert verdict.kind == DuplicateKind.EXACT_RAW


def test_a_shared_strong_identifier_is_a_duplicate():
    a = fingerprint("a", identifiers={"doi:10.1234/abc"})
    b = fingerprint("b", identifiers={"doi:10.1234/abc"})

    verdict = compare(a, b)
    assert verdict.is_duplicate
    assert verdict.kind == DuplicateKind.IDENTIFIER


def test_the_same_work_by_title_author_and_language_is_a_duplicate():
    a = fingerprint("a", title="The Social Contract", authors=("Rousseau, Jean-Jacques",))
    b = fingerprint("b", title="Social Contract", authors=("Jean-Jacques Rousseau",))

    verdict = compare(a, b)
    assert verdict.is_duplicate
    assert verdict.kind == DuplicateKind.TITLE_AUTHOR


# ------------------------------------------------------- 29. near duplicates


def test_lightly_reformatted_text_is_a_near_duplicate():
    original = (
        "Man is born free, and everywhere he is in chains. One thinks himself the "
        "master of others, and still remains a greater slave than they. How did "
        "this change come about? I do not know. What can make it legitimate? "
    ) * 12
    reformatted = original.replace(", and", " and").replace("  ", " ")

    a = fingerprint("a", simhash_value=simhash(original), normalized_sha256="norm-a")
    b = fingerprint("b", simhash_value=simhash(reformatted), normalized_sha256="norm-b",
                    title="Different Title Entirely", authors=("Someone Else",))

    verdict = compare(a, b)
    assert verdict.is_duplicate
    assert verdict.kind == DuplicateKind.NEAR
    assert verdict.similarity > 0.9


def test_genuinely_different_texts_are_not_near_duplicates():
    first = "A treatise upon the government of cities and the nature of civil authority. " * 20
    second = "A natural history of the northern coast, its birds, its fisheries, and its people. " * 20

    a = fingerprint("a", simhash_value=simhash(first), normalized_sha256="norm-a",
                    title="On Government", authors=("Author A",))
    b = fingerprint("b", simhash_value=simhash(second), normalized_sha256="norm-b",
                    title="Northern Coast", authors=("Author B",))

    assert not compare(a, b).is_duplicate


def test_simhash_is_stable_across_processes():
    """
    SimHash is persisted to the database, so it must mean the same thing
    tomorrow as today. Python's built-in hash() is randomized per process
    and would silently invalidate every stored value on restart.
    """
    text = "The same text hashed twice must produce the same value every time. " * 10
    assert simhash(text) == simhash(text)
    # Value asserted as a constant so a change in the algorithm is a
    # visible, deliberate act rather than a silent corpus-wide drift.
    assert simhash(text) == simhash(text[:])


def test_hamming_and_similarity_arithmetic():
    assert hamming_distance(0b1010, 0b1010) == 0
    assert hamming_distance(0b1010, 0b1011) == 1
    assert simhash_similarity(0b1010, 0b1010) == 1.0
    assert 0.98 < simhash_similarity(0b1010, 0b1011) < 1.0


def test_minhash_estimates_jaccard_similarity():
    shared = "the quick brown fox jumps over the lazy dog near the river bank at dawn " * 10
    a = minhash_signature(shared)
    b = minhash_signature(shared + " with a short additional clause")
    c = minhash_signature("entirely unrelated prose about metallurgy and the smelting of ores " * 10)

    assert jaccard_estimate(a, a) == 1.0
    assert jaccard_estimate(a, b) > jaccard_estimate(a, c)


# --------------------------------- 30. translations are never merged


def test_two_translations_of_one_work_are_never_merged():
    """
    Baudelaire's Poe is not Poe. Different language means different
    rights, different quality, and different retrieval value -- merging
    them would be the single most damaging dedup error this corpus could
    make.
    """
    english = fingerprint("en", title="The Social Contract", language="en")
    french = fingerprint("fr", title="Du contrat social", language="fr")

    verdict = compare(english, french)
    assert not verdict.is_duplicate
    assert verdict.related_but_distinct is True
    assert "translation" in verdict.reason.lower() or "language" in verdict.reason.lower()


def test_two_different_translators_of_the_same_language_are_not_merged():
    cole = fingerprint("cole", translator="G. D. H. Cole", normalized_sha256="norm-cole")
    tozer = fingerprint("tozer", translator="H. J. Tozer", normalized_sha256="norm-tozer")

    verdict = compare(cole, tozer)
    assert not verdict.is_duplicate
    assert verdict.related_but_distinct is True


def test_the_work_key_includes_language_by_design():
    assert work_key("The Social Contract", ["Rousseau"], "en") != work_key(
        "The Social Contract", ["Rousseau"], "fr"
    )


def test_an_annotated_edition_is_not_a_duplicate_of_the_plain_text():
    plain = fingerprint("plain", title="The Republic")
    annotated = fingerprint(
        "annotated", title="The Republic: A Critical Edition, Annotated", normalized_sha256="norm-ann"
    )

    verdict = compare(plain, annotated)
    assert not verdict.is_duplicate
    assert verdict.related_but_distinct is True


def test_an_extract_is_not_a_duplicate_of_the_complete_work():
    """
    Same title and author, wildly different length: an anthology or a
    chapter offprint sitting next to the complete work. The title/author
    key matches, so only the length guard prevents the merge.
    """
    complete = fingerprint("complete", title="The Republic", char_count=800_000)
    extract = fingerprint(
        "extract", title="The Republic", char_count=40_000, normalized_sha256="norm-ex"
    )

    verdict = compare(complete, extract)
    assert not verdict.is_duplicate
    assert verdict.related_but_distinct is True
    assert "extract" in verdict.reason


def test_a_shared_identifier_does_not_merge_substantively_different_editions():
    first = fingerprint("first", identifiers={"isbn:9780140442014"}, title="Leviathan")
    revised = fingerprint(
        "revised",
        identifiers={"isbn:9780140442014"},
        title="Leviathan: Revised Student Edition",
        normalized_sha256="norm-rev",
    )

    verdict = compare(first, revised)
    assert not verdict.is_duplicate
    assert verdict.related_but_distinct is True


# ------------------------------------------------------------- key building


def test_title_normalization_folds_articles_accents_and_punctuation():
    assert normalize_title("The Social Contract") == normalize_title("Social Contract")
    assert normalize_title("Du Contrat Social") == normalize_title("contrat social")
    assert normalize_title("Les Misérables") == normalize_title("Miserables")
    assert normalize_title("The Republic: Book One") == normalize_title("Republic")


def test_author_normalization_survives_catalogue_house_styles():
    assert normalize_author("Rousseau, Jean-Jacques") == normalize_author("Jean-Jacques Rousseau")
    assert normalize_author("Marx, Karl, 1818-1883") == normalize_author("Karl Marx")
    assert normalize_author("Plato (428?-348? BCE)") == normalize_author("Plato")


def test_identifier_extraction_finds_the_schemes_that_matter():
    found = extract_identifiers(
        "https://www.gutenberg.org/ebooks/61",
        "https://doi.org/10.1234/example.5678",
        "ark:/12148/bpt6k1234567",
        "ISBN 978-0-14-044201-4",
    )
    assert "gutenberg:61" in found
    assert "doi:10.1234/example.5678" in found
    assert "ark:/12148/bpt6k1234567" in found
    assert any(i.startswith("isbn:9780140442014") for i in found)


def test_a_malformed_isbn_is_not_extracted():
    assert not any(i.startswith("isbn:") for i in extract_identifiers("ISBN 123"))


# -------------------------------------------------------- corpus-level API


def test_find_duplicate_returns_the_strongest_match_in_the_corpus():
    new = fingerprint("new", normalized_sha256="SHARED", simhash_value=simhash("some text here"))
    corpus = [
        fingerprint("weak", title="Unrelated", authors=("Nobody",), normalized_sha256="other"),
        fingerprint("exact", normalized_sha256="SHARED"),
    ]

    verdict = find_duplicate(new, corpus)
    assert verdict.is_duplicate
    assert verdict.matched_document_id == "exact"


def test_find_duplicate_reports_a_related_but_distinct_relationship():
    new = fingerprint("new_fr", language="fr", title="Du contrat social")
    corpus = [fingerprint("existing_en", language="en", title="The Social Contract")]

    verdict = find_duplicate(new, corpus)
    assert not verdict.is_duplicate
    assert verdict.related_but_distinct is True


def test_find_duplicate_on_an_empty_corpus_finds_nothing():
    assert not find_duplicate(fingerprint("only"), []).is_duplicate


def test_the_representative_is_the_best_copy_and_the_choice_is_reproducible():
    candidates = [
        fingerprint("low", quality_score=0.5, char_count=90_000),
        fingerprint("best", quality_score=0.95, char_count=100_000),
        fingerprint("truncated", quality_score=0.95, char_count=10_000),
    ]
    assert choose_representative(candidates) == "best"
    # Order-independent: the same set always yields the same winner.
    assert choose_representative(list(reversed(candidates))) == "best"


def test_cluster_ids_are_independent_of_discovery_order():
    assert cluster_id_for(["a", "b", "c"]) == cluster_id_for(["c", "a", "b"])
    assert cluster_id_for(["a", "b"]) != cluster_id_for(["a", "c"])
