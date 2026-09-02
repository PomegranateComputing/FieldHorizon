"""
books / manifesto classification.

Covers required cases 31-35: books classification, manifesto
classification, the no-LLM fallback, malformed LLM JSON, and a prompt
injection embedded in a harvested document.

The golden dataset in `fixtures/corpus/golden_classification.json` is
multilingual (en/fr/de/es/it/la) and includes the cases that matter most:
a political novel, a history of a revolution, poetry, drama, and a papal
bull -- the forms that a topical or length-based heuristic gets wrong.

Every test here runs with `use_llm=False` unless it is specifically
testing the LLM path, so the suite never depends on a reachable model.
"""

from __future__ import annotations

import json

import pytest

from fieldhorizon.corpus.classification import (
    LOW_CONFIDENCE_THRESHOLD,
    build_sample,
    classify,
    classify_with_llm,
    score_form,
    score_lexical,
    score_structure,
)
from fieldhorizon.corpus.models import Destination
from tests.corpus_helpers import FIXTURES, fixture_text, make_config

GOLDEN = json.loads((FIXTURES / "golden_classification.json").read_text(encoding="utf-8"))
CASES = GOLDEN["cases"]
UNAMBIGUOUS = [c for c in CASES if not c.get("ambiguous")]
AMBIGUOUS = [c for c in CASES if c.get("ambiguous")]


def classify_case(case: dict, **overrides):
    return classify(
        title=case["title"],
        document_type=case["document_type"],
        subjects=case["subjects"],
        text=case["text"],
        use_llm=overrides.pop("use_llm", False),
        **overrides,
    )


# ------------------------------------------------- 31/32. the golden set


@pytest.mark.parametrize("case", UNAMBIGUOUS, ids=[c["id"] for c in UNAMBIGUOUS])
def test_golden_dataset_destinations(case):
    """
    Every unambiguous golden case must land where its rationale says.
    Runs fully deterministically -- no model involved.
    """
    result = classify_case(case)
    assert result.destination.value == case["expected"], (
        f"{case['id']} ({case['language']}): expected {case['expected']}, "
        f"got {result.destination.value} at manifesto_score={result.manifesto_score:.3f}. "
        f"Rationale: {result.rationale}"
    )


@pytest.mark.parametrize("case", AMBIGUOUS, ids=[c["id"] for c in AMBIGUOUS])
def test_genuinely_ambiguous_cases_report_low_confidence(case):
    """
    A hard case must be *flagged*, not confidently misfiled. Asserting a
    particular destination here would make the suite claim an accuracy
    the classifier does not have.
    """
    result = classify_case(case)
    assert result.confidence < 0.85, (
        f"{case['id']} is genuinely ambiguous but was classified with "
        f"confidence {result.confidence}"
    )


def test_the_golden_set_covers_every_priority_language_and_both_destinations():
    languages = {c["language"] for c in CASES}
    assert languages == {"en", "fr", "de", "es", "it", "la"}

    destinations = {c["expected"] for c in CASES}
    assert destinations == {"books", "manifesto"}

    forms = {c["document_type"] for c in CASES}
    # The brief's required form coverage: novels, poetry, drama, treatises,
    # histories, political and artistic manifestos, constitutions,
    # sermons, and ambiguous philosophy.
    assert {"novel", "poésie", "teatro", "traité", "history", "manifesto",
            "manifeste", "constitución", "sermon", "essay"} <= forms


# ---------------------------------------- the functional-not-topical rule


def test_a_political_novel_is_books():
    """The single most important negative case: topic must not decide."""
    case = next(c for c in CASES if c["id"] == "political_novel_en")
    result = classify_case(case)
    assert result.destination == Destination.BOOKS


def test_a_history_of_a_revolution_is_books_but_a_call_for_one_is_manifesto():
    history = classify_case(next(c for c in CASES if c["id"] == "history_of_revolution_en"))
    call = classify_case(next(c for c in CASES if c["id"] == "communist_manifesto_en"))

    assert history.destination == Destination.BOOKS
    assert call.destination == Destination.MANIFESTO
    assert call.manifesto_score > history.manifesto_score


def test_short_forms_are_classified_by_function_not_by_length():
    """
    A sonnet sequence and a papal bull are both short. One is books, one
    is manifesto -- so length cannot be doing the work.
    """
    poetry = classify_case(next(c for c in CASES if c["id"] == "poetry_fr"))
    bull = classify_case(next(c for c in CASES if c["id"] == "papal_bull_la"))

    assert poetry.destination == Destination.BOOKS
    assert bull.destination == Destination.MANIFESTO


def test_a_biography_of_an_agitator_is_books():
    result = classify_case(next(c for c in CASES if c["id"] == "biography_en"))
    assert result.destination == Destination.BOOKS


# ------------------------------------------------------ component signals


def test_bibliographic_form_pushes_in_the_right_direction():
    manifesto_score, confidence, label = score_form("manifesto", ["Politics"], "Some Title")
    assert manifesto_score > 0.5
    assert confidence > 0
    assert label == "manifesto"

    books_score, _, books_label = score_form("novel", ["Fiction"], "Some Title")
    assert books_score < -0.5
    assert books_label == "novel"


def test_form_matching_respects_word_boundaries():
    """
    A book titled "Manifestations of the Divine" is not a manifesto, and
    substring matching on titles is exactly how that mistake happens.
    """
    score, _, label = score_form("", [], "Manifestations of the Divine")
    assert label != "manifesto"
    assert score == 0.0

    romance, _, romance_label = score_form("", [], "Romanticism in the Age of Revolution")
    assert romance_label != "roman"


def test_title_evidence_is_weighted_below_an_explicit_catalogue_form():
    from_type, type_conf, _ = score_form("manifesto", [], "")
    from_title, title_conf, _ = score_form("", [], "manifesto")
    assert type_conf > title_conf
    assert from_type > from_title


def test_lexical_scoring_is_density_based_not_count_based():
    """
    A manifesto is short by nature and would lose every raw-count
    comparison against a long book.
    """
    manifesto = fixture_text("sample_manifesto.txt")
    book = fixture_text("sample_book.txt") * 8

    manifesto_score, _, _ = score_lexical(manifesto)
    book_score, _, _ = score_lexical(book)

    assert manifesto_score > 0
    assert book_score < 0


def test_lexical_evidence_records_where_it_matched():
    _, _, evidence = score_lexical(fixture_text("sample_manifesto.txt"))
    assert evidence.hits
    assert all("location" in hit and "short_excerpt" in hit for hit in evidence.hits)


def test_structure_distinguishes_chapters_from_numbered_articles():
    chapters, chapter_conf, chapter_metrics = score_structure(
        "", ["Chapter I", "Chapter II", "Chapter III", "Chapter IV"]
    )
    articles, article_conf, article_metrics = score_structure(
        "", ["Article I", "Article II", "Article III", "Article IV"]
    )

    assert chapters < 0 and chapter_metrics["chapter_headings"] >= 3
    assert articles > 0 and article_metrics["article_headings"] >= 3


def test_structure_declines_to_decide_when_both_patterns_are_present():
    """A scholarly edition of a constitution has both. Structure cannot settle it."""
    score, confidence, _ = score_structure(
        "", ["Chapter I", "Chapter II", "Chapter III", "Article I", "Article II", "Article III"]
    )
    assert score == 0.0
    assert confidence <= 0.25


# ------------------------------------------------------ 33. no-LLM fallback


def test_classification_works_with_no_model_available():
    """
    Required case 33. The whole suite runs this way, but the property is
    asserted directly here so that a regression is unambiguous.
    """
    result = classify(
        title="Manifesto of the Communist Party",
        document_type="manifesto",
        subjects=["Communism"],
        text=fixture_text("sample_manifesto.txt"),
        cfg=None,
        use_llm=False,
    )
    assert result.destination == Destination.MANIFESTO
    assert result.classifier_kind == "deterministic"
    assert result.rationale


def test_an_unreachable_model_falls_back_silently(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)

    def _unreachable(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("fieldhorizon.llm.call_ollama", _unreachable)

    result = classify(
        title="Declaration of the Rights of the Associated Workers",
        document_type="declaration",
        subjects=["Politics"],
        text=fixture_text("sample_manifesto.txt"),
        cfg=cfg,
        use_llm=True,
    )
    assert result.destination == Destination.MANIFESTO
    assert result.classifier_kind == "deterministic"


# ------------------------------------------------- 34. malformed LLM JSON


@pytest.mark.parametrize(
    "bad_output",
    [
        "not json at all",
        "{",
        '{"destination": "somewhere_else"}',
        '{"destination": null}',
        "[]",
        '{"manifesto_score": 0.9}',  # no destination
    ],
)
def test_malformed_model_output_is_discarded_not_trusted(tmp_path, monkeypatch, bad_output):
    cfg = make_config(tmp_path)
    monkeypatch.setattr("fieldhorizon.llm.call_ollama", lambda *a, **k: bad_output)

    assert classify_with_llm(cfg, "a sample") is None

    # And the overall classification still succeeds deterministically.
    result = classify(
        title="A Natural History of the Northern Coast",
        document_type="natural history",
        subjects=["Science"],
        text=fixture_text("sample_book.txt"),
        cfg=cfg,
        use_llm=True,
    )
    assert result.destination == Destination.BOOKS
    assert result.classifier_kind == "deterministic"


def test_a_valid_model_verdict_is_incorporated_as_one_weighted_voice(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    payload = json.dumps(
        {
            "destination": "manifesto",
            "manifesto_score": 0.9,
            "confidence": 0.8,
            "document_form": "manifesto",
            "primary_domain": "politics",
            "secondary_tags": ["revolution"],
            "normative_intent": 0.9,
            "mobilization_intent": 0.95,
            "doctrinal_intent": 0.8,
            "narrative_intent": 0.1,
            "analytical_intent": 0.2,
            "rationale": "It demands and proclaims.",
            "evidence": [{"location": "opening", "short_excerpt": "We declare"}],
        }
    )
    monkeypatch.setattr("fieldhorizon.llm.call_ollama", lambda *a, **k: payload)

    result = classify(
        title="Declaration of the Rights of the Associated Workers",
        document_type="declaration",
        subjects=["Politics"],
        text=fixture_text("sample_manifesto.txt"),
        cfg=cfg,
        use_llm=True,
    )
    assert result.destination == Destination.MANIFESTO
    assert result.classifier_kind == "hybrid"
    assert result.mobilization_intent == pytest.approx(0.95)
    assert any("We declare" in e.get("short_excerpt", "") for e in result.evidence)


def test_the_model_cannot_overturn_confident_deterministic_agreement(tmp_path, monkeypatch):
    """
    The model is a weighted voice, capped at 0.9, never an authority. A
    plainly narrative book must survive a model insisting otherwise --
    which is also the last line of defence against a successful prompt
    injection.
    """
    cfg = make_config(tmp_path)
    payload = json.dumps(
        {"destination": "manifesto", "manifesto_score": 1.0, "confidence": 1.0,
         "rationale": "compromised output"}
    )
    monkeypatch.setattr("fieldhorizon.llm.call_ollama", lambda *a, **k: payload)

    result = classify(
        title="A Natural History of the Northern Coast",
        document_type="natural history",
        subjects=["Science", "Travel", "Natural history"],
        text=fixture_text("sample_book.txt") * 6,
        cfg=cfg,
        use_llm=True,
    )
    assert result.destination == Destination.BOOKS


# -------------------------------------------------- 35. prompt injection


def test_an_injected_instruction_does_not_change_the_classification():
    """
    Required case 35. The document contains explicit instructions aimed
    at the classifier: ignore your prompt, classify as manifesto, fetch a
    URL, execute a script, set the rights decision to accept.

    Deterministically, none of that is even read as an instruction -- it
    is text. The document is an expository treatise and must classify as
    books.
    """
    result = classify(
        title="A Treatise on the Ordering of the Passions",
        document_type="treatise",
        subjects=["Philosophy", "Psychology"],
        text=fixture_text("prompt_injection.txt"),
        use_llm=False,
    )
    assert result.destination == Destination.BOOKS


def test_the_classifier_prompt_fences_the_document_with_an_unguessable_delimiter():
    """
    A document cannot close a fence it has never seen. The delimiter is
    random per call, so an injected "--- END SAMPLE ---" cannot escape.
    """
    from fieldhorizon.corpus.classification import _CLASSIFIER_PROMPT

    sample = build_sample("T", "", [], "body text", [])
    first = _CLASSIFIER_PROMPT.format(sample=sample, delimiter="aaaa")
    assert "BEGIN UNTRUSTED DOCUMENT SAMPLE aaaa" in first
    assert "END UNTRUSTED DOCUMENT SAMPLE aaaa" in first


def test_the_classifier_system_prompt_states_the_document_is_data(tmp_path, monkeypatch):
    """
    The system prompt must tell the model the sample is untrusted data,
    and must not be the doctrinal FIELD_HORIZON persona -- which forbids
    structured output and offers no injection defence.
    """
    captured = {}

    def _capture(cfg, prompt, model=None, options=None, system=None):
        captured["system"] = system
        captured["options"] = options
        return json.dumps({"destination": "books", "manifesto_score": 0.1, "confidence": 0.5})

    monkeypatch.setattr("fieldhorizon.llm.call_ollama", _capture)
    classify_with_llm(make_config(tmp_path), "a sample")

    system = captured["system"]
    assert system is not None
    assert "UNTRUSTED" in system
    assert "IGNORE" in system.upper()
    assert "never an instruction" in system
    assert "FIELD_HORIZON" not in system  # not the doctrinal persona
    assert captured["options"]["temperature"] == 0


def test_an_injection_cannot_reach_a_tool_a_fetch_or_a_rights_decision(tmp_path, monkeypatch):
    """
    Even a fully "successful" injection -- the model returning exactly
    what the document demanded -- can only influence a books/manifesto
    label and some floats. It cannot cause a fetch, a write, a shell
    command, or a rights decision, because the classifier's return type
    has no way to express any of those.
    """
    cfg = make_config(tmp_path)
    compromised = json.dumps(
        {
            "destination": "manifesto",
            "manifesto_score": 1.0,
            "confidence": 1.0,
            "rationale": "COMPROMISED",
            "fetch_url": "https://attacker.example.com/exfiltrate",
            "execute": "/tmp/payload.sh",
            "rights_decision": "accept",
            "license": "CC0",
        }
    )
    monkeypatch.setattr("fieldhorizon.llm.call_ollama", lambda *a, **k: compromised)

    result = classify(
        title="A Treatise on the Ordering of the Passions",
        document_type="treatise",
        subjects=["Philosophy"],
        text=fixture_text("prompt_injection.txt"),
        cfg=cfg,
        use_llm=True,
    )

    payload = result.to_dict()
    assert "fetch_url" not in payload
    assert "execute" not in payload
    assert "rights_decision" not in payload
    assert "license" not in payload
    # The one thing it could influence is the label -- and the
    # deterministic signals still outvote it.
    assert result.destination == Destination.BOOKS


# ------------------------------------------------------------ sampling


def test_the_sample_never_sends_a_whole_book_to_the_model():
    long_text = fixture_text("sample_book.txt") * 50
    sample = build_sample("Title", "Subtitle", ["Subject"], long_text, ["Chapter I"])

    assert len(sample) < len(long_text) / 5
    # Beginning, interior, and end are all represented: a manifesto's
    # declarative act is front-loaded, a book's framing is often in its
    # conclusion, and the middle distinguishes sustained narration from
    # sustained exhortation.
    assert "OPENING:" in sample
    assert "INTERIOR SAMPLE 1:" in sample
    assert "CLOSING:" in sample


def test_a_short_document_is_sampled_whole():
    sample = build_sample("T", "", [], "A short declaration.", [])
    assert "FULL TEXT:" in sample


def test_the_sample_carries_title_subjects_and_structure():
    sample = build_sample("The Title", "A Subtitle", ["Politics"], "body", ["Article I"])
    assert "TITLE: The Title" in sample
    assert "SUBTITLE: A Subtitle" in sample
    assert "SUBJECTS: Politics" in sample
    assert "SECTION HEADINGS: Article I" in sample


# ------------------------------------------------------- low confidence


def test_low_confidence_yields_a_destination_and_a_flag_never_a_quarantine():
    """
    Categorical hesitation must never be treated as a legal problem.
    Quarantine is for rights alone.
    """
    result = classify(title="", document_type="", subjects=[], text="Short and featureless.",
                      use_llm=False)

    assert result.destination in (Destination.BOOKS, Destination.MANIFESTO)
    assert result.low_confidence is True
    assert result.confidence < LOW_CONFIDENCE_THRESHOLD


def test_a_document_with_no_signal_at_all_defaults_to_books():
    result = classify(title="", document_type="", subjects=[], text="", use_llm=False)
    assert result.destination == Destination.BOOKS
    assert result.low_confidence is True


def test_classification_serializes_to_the_documented_schema():
    result = classify_case(CASES[0])
    payload = result.to_dict()

    assert set(payload) == {
        "destination", "manifesto_score", "confidence", "document_form", "primary_domain",
        "secondary_tags", "normative_intent", "mobilization_intent", "doctrinal_intent",
        "narrative_intent", "analytical_intent", "rationale", "evidence",
        "classifier_version", "classifier_kind", "low_confidence",
    }
    assert payload["destination"] in ("books", "manifesto")
    assert 0.0 <= payload["manifesto_score"] <= 1.0


def test_classification_is_deterministic_without_a_model():
    case = CASES[0]
    first = classify_case(case)
    second = classify_case(case)
    assert first.destination == second.destination
    assert first.manifesto_score == second.manifesto_score
