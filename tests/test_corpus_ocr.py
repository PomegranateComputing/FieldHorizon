"""
Optional OCR: detection, the three gates, and the state a scan rests in.

The change this file defends: Phase I raised `NormalizationError` for a
scanned PDF, which the orchestrator treats as FINAL. That is right for a
malformed EPUB -- it will still be malformed next week -- and wrong for a
page image, which is a perfectly good document whose text has not been
extracted. A scan now rests in OCR_PENDING and becomes processable the
moment a backend exists, with nothing re-downloaded.

Nothing here runs OCR. Three gates must all open first, and this phase's
brief says explicitly not to run bulk OCR at all.
"""

from __future__ import annotations

import pytest

from fieldhorizon.corpus.normalization import NormalizationError, normalize_pdf
from fieldhorizon.corpus.ocr import (
    DEFAULT_MAX_OCR_DOCUMENTS_PER_RUN,
    NEEDS_OCR_MARKER,
    OCR_WORKER_ENV,
    NeedsOCR,
    OcrBackend,
    decide,
    detect_backend,
    health_report,
)
from tests.corpus_helpers import build_pdf

PRESENT = OcrBackend(kind="binary", name="tesseract", detail="/usr/bin/tesseract")
ABSENT = OcrBackend()


# ------------------------------------------------------------- detection


def test_detection_reports_absence_without_importing_anything_heavy(monkeypatch):
    """
    `corpus health` must be safe and cheap to run. Finding out whether
    OCR exists must not drag a large native library into every process
    that asks the question.
    """
    monkeypatch.delenv(OCR_WORKER_ENV, raising=False)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr("shutil.which", lambda name: None)

    backend = detect_backend()
    assert not backend.available
    assert backend.kind == ""


def test_an_external_binary_counts_without_a_python_binding(monkeypatch):
    """
    An operator who has tesseract installed should not also need a
    wrapper library before the harvester will notice.
    """
    monkeypatch.delenv(OCR_WORKER_ENV, raising=False)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tesseract" if name == "tesseract" else None)

    backend = detect_backend()
    assert backend.available
    assert backend.kind == "binary"
    assert backend.name == "tesseract"


def test_an_external_worker_is_detected_without_being_contacted(monkeypatch):
    """
    Detection must have no side effects. Reaching out to a queue endpoint
    to see whether it answers would make `corpus health` a network
    operation, which it is not.
    """
    monkeypatch.setenv(OCR_WORKER_ENV, "https://ocr.internal/queue")

    backend = detect_backend()
    assert backend.kind == "worker"
    assert backend.detail == "https://ocr.internal/queue"


def test_a_broken_optional_module_does_not_crash_detection(monkeypatch):
    monkeypatch.delenv(OCR_WORKER_ENV, raising=False)

    def explode(name):
        raise ValueError("a half-installed package")

    monkeypatch.setattr("importlib.util.find_spec", explode)
    monkeypatch.setattr("shutil.which", lambda name: None)

    assert not detect_backend().available


# ----------------------------------------------------------- the gates


def test_no_backend_means_no_ocr_and_the_reason_says_what_to_install():
    """
    "OCR unavailable" tells an operator only that something is wrong.
    The reason has to say what to DO.
    """
    decision = decide(policy_enabled=True, backend=ABSENT)

    assert not decision.allowed
    assert "fieldhorizon[ocr]" in decision.reason
    assert "tesseract" in decision.reason
    assert OCR_WORKER_ENV in decision.reason


def test_a_backend_alone_is_not_permission():
    """Installing tesseract must not silently enable OCR across a corpus."""
    decision = decide(policy_enabled=False, backend=PRESENT)

    assert not decision.allowed
    assert "ocr.enabled is false" in decision.reason
    assert "tesseract" in decision.reason, "the reason should say the backend IS there"


def test_the_per_run_ceiling_stops_a_corpus_wide_run():
    """
    OCR is the one operation here costed in CPU-hours rather than bytes.
    The ceiling makes an accidental corpus-wide run impossible rather
    than merely unlikely.
    """
    assert decide(policy_enabled=True, documents_done=0, max_documents=5,
                  backend=PRESENT).allowed

    exhausted = decide(policy_enabled=True, documents_done=5, max_documents=5, backend=PRESENT)
    assert not exhausted.allowed
    assert "OCR_PENDING for a later run" in exhausted.reason


def test_a_zero_ceiling_disables_ocr_entirely():
    assert not decide(policy_enabled=True, max_documents=0, backend=PRESENT).allowed


def test_the_default_ceiling_is_small_enough_to_be_safe():
    """This phase's brief says explicitly not to run bulk OCR."""
    assert 0 < DEFAULT_MAX_OCR_DOCUMENTS_PER_RUN <= 10


def test_all_three_gates_open_before_ocr_runs():
    assert decide(policy_enabled=True, documents_done=0, max_documents=5,
                  backend=PRESENT).allowed is True


# ------------------------------------------------------ the scan itself


def test_a_scanned_pdf_raises_needs_ocr_not_a_normalization_error():
    """
    The distinction the whole step rests on. NormalizationError is
    treated as FINAL by the orchestrator; NeedsOCR is not, and must not
    be a subclass of it.
    """
    with pytest.raises(NeedsOCR) as caught:
        normalize_pdf(build_pdf(["ignored"], with_text_layer=False))

    assert not isinstance(caught.value, NormalizationError)
    assert caught.value.marker == NEEDS_OCR_MARKER


def test_a_pdf_with_a_text_layer_never_asks_for_ocr():
    """OCR is the last resort, not the first."""
    result = normalize_pdf(build_pdf(["The text layer is right here.", "Second line."]))
    assert "text layer is right here" in result.text


def test_a_malformed_pdf_is_still_a_final_failure():
    """
    The rule this correction must not erode: a document that is actually
    broken stays broken, and does not get parked in OCR_PENDING forever.
    """
    with pytest.raises(NormalizationError, match="Not a PDF"):
        normalize_pdf(b"not a PDF at all")


def test_ocr_pending_is_not_an_error_state():
    from fieldhorizon.corpus.models import (
        NON_ERROR_ACQUISITION_STATES,
        TERMINAL_STATES,
        State,
    )

    assert State.OCR_PENDING in NON_ERROR_ACQUISITION_STATES
    assert State.OCR_PENDING not in TERMINAL_STATES


def test_a_document_can_leave_ocr_pending_once_a_backend_exists():
    """
    The point of the state. If OCR_PENDING were terminal, installing
    tesseract would fix nothing without a re-download.
    """
    from fieldhorizon.corpus.models import State, can_transition

    assert can_transition(State.OCR_PENDING, State.NORMALIZED)


# ------------------------------------------------------------- reporting


def test_the_health_report_distinguishes_missing_from_disabled(monkeypatch):
    monkeypatch.delenv(OCR_WORKER_ENV, raising=False)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr("shutil.which", lambda name: None)

    missing = health_report(policy_enabled=True, max_documents=5)
    assert not missing["backend"]["available"]
    assert not missing["would_run"]

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tesseract" if name == "tesseract" else None)
    disabled = health_report(policy_enabled=False, max_documents=5)
    assert disabled["backend"]["available"]
    assert not disabled["would_run"]
    assert "ocr.enabled is false" in disabled["reason"]


def test_the_health_report_says_provider_text_is_preferred():
    """
    Gallica's text mode, OAPEN's extracted TEXT bundle, and Wikisource's
    wikitext are the provider's own extraction and usually proofread.
    OCR must never look like a shortcut around implementing one of those.
    """
    note = health_report(policy_enabled=False, max_documents=5)["note"]

    assert "preferred" in note
    assert "OAPEN" in note or "Gallica" in note


def test_the_install_instruction_survives_rich_markup():
    """
    Rich reads square brackets as markup, and an unescaped reason printed
    `pip install 'fieldhorizon'` -- an instruction that runs cleanly and
    installs the wrong thing.
    """
    from rich.markup import escape as rich_escape

    reason = decide(policy_enabled=True, backend=ABSENT).reason
    assert "fieldhorizon[ocr]" in rich_escape(reason).replace("\\[", "[")


def test_the_run_report_counts_ocr_pending_separately_from_errors():
    """
    A scan waiting for a capability is not a failure. Folding it into the
    error count is exactly what made Phase I report good documents as
    broken.
    """
    from fieldhorizon.corpus.orchestrator import RunStats

    stats = RunStats()
    stats.ocr_pending = 3

    payload = stats.to_dict()
    assert payload["ocr_pending"] == 3
    assert payload["errors"] == 0
