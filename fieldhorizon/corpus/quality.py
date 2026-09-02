"""
Text quality scoring.

The single most important design point here is the one the brief calls
out explicitly: **no blind length threshold**. A declaration of the
rights of man, a papal bull, a Dada manifesto, and a sonnet are all short
and all exactly the sort of document this corpus exists to hold. A
minimum-word-count gate would delete the manifesto half of the corpus
while congratulating itself on quality.

So length is scored *against the document's expected form*: a text
presenting itself as a novel that yields 400 words has failed to
download; a proclamation that yields 400 words is a proclamation.

Rejected documents are kept as records with their reason, never deleted
-- a source that starts producing bad OCR should be visible as a pattern,
which is impossible if its failures vanish.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .language import detect_language, normalize_language_code
from .models import QualityReport

#: Forms that are legitimately short. Matched against the catalogue's
#: document_type and the classifier's document_form.
SHORT_FORM_TYPES = frozenset(
    {
        "manifesto", "manifeste", "declaration", "déclaration", "proclamation",
        "charter", "charte", "constitution", "speech", "discours", "address",
        "poem", "poetry", "poème", "sonnet", "tract", "pamphlet", "broadside",
        "creed", "credo", "thesis", "theses", "sermon", "letter", "lettre",
        "decree", "edict", "édit", "bull", "encyclical", "platform", "programme",
    }
)

#: Below this, nothing can be assessed at all -- there is no document.
ABSOLUTE_MIN_CHARS = 200

#: Expected minimum for a text claiming to be a full-length book.
LONG_FORM_MIN_CHARS = 20_000

_WORD_RE = re.compile(r"\S+")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

#: Replacement characters and the classic mojibake bigrams. Their density
#: is the most reliable single indicator of a botched decode.
_UNREADABLE_RE = re.compile(r"[�￾￿]")
_MOJIBAKE_RE = re.compile(r"Ã[©¨ ¢£¤¥¦§¬­®¯°±\x80-\xbf]|â€[™œ\x9d]|Ã¯Â¿Â½")


@dataclass(frozen=True)
class QualityThresholds:
    """Configurable gate. Defaults are deliberately forgiving of old texts."""

    min_score: float = 0.45
    max_unreadable_ratio: float = 0.01
    max_mojibake_ratio: float = 0.002
    min_alpha_ratio: float = 0.55
    max_repetition_ratio: float = 0.35
    min_language_confidence: float = 0.35
    #: Whether a detected language contradicting the declared one is
    #: fatal. Off by default: catalogues mislabel language constantly,
    #: especially for multilingual and Latin texts, and the detected value
    #: is what gets stored either way.
    reject_on_language_mismatch: bool = False


def score_quality(
    text: str,
    *,
    declared_language: str = "",
    document_type: str = "",
    source_format: str = "",
    format_metrics: dict | None = None,
    thresholds: QualityThresholds | None = None,
) -> QualityReport:
    """
    Score a normalized text in 0..1, with the issues that cost it points.

    Scoring is subtractive from 1.0 so that the reasons are always
    legible: a report saying `score=0.42, issues=["mojibake_density",
    "ocr_fragmentation"]` tells an operator what to fix, which a bare
    aggregate never does.
    """
    thresholds = thresholds or QualityThresholds()
    format_metrics = format_metrics or {}
    issues: list[str] = []
    metrics: dict = {}

    chars = len(text)
    words = _WORD_RE.findall(text)
    word_count = len(words)

    if chars < ABSOLUTE_MIN_CHARS:
        return QualityReport(
            score=0.0,
            language_detected="unknown",
            language_confidence=0.0,
            char_count=chars,
            word_count=word_count,
            issues=("empty_or_negligible_text",),
            metrics={"char_count": chars},
        )

    score = 1.0

    # --- decode integrity ------------------------------------------------
    unreadable = len(_UNREADABLE_RE.findall(text))
    unreadable_ratio = unreadable / chars
    metrics["unreadable_ratio"] = round(unreadable_ratio, 6)
    if unreadable_ratio > thresholds.max_unreadable_ratio:
        issues.append("unreadable_characters")
        score -= min(0.5, unreadable_ratio * 20)

    mojibake = len(_MOJIBAKE_RE.findall(text))
    mojibake_ratio = mojibake / max(1, word_count)
    metrics["mojibake_ratio"] = round(mojibake_ratio, 6)
    if mojibake_ratio > thresholds.max_mojibake_ratio:
        issues.append("mojibake_density")
        score -= min(0.4, mojibake_ratio * 30)

    # --- is this actually prose? -----------------------------------------
    letters = len(_LETTER_RE.findall(text))
    alpha_ratio = letters / chars
    metrics["alpha_ratio"] = round(alpha_ratio, 4)
    if alpha_ratio < thresholds.min_alpha_ratio:
        issues.append("low_alphabetic_density")
        score -= min(0.4, (thresholds.min_alpha_ratio - alpha_ratio) * 2)

    # --- repetition ------------------------------------------------------
    repetition = _repetition_ratio(text)
    metrics["repetition_ratio"] = round(repetition, 4)
    if repetition > thresholds.max_repetition_ratio:
        issues.append("high_line_repetition")
        score -= min(0.35, (repetition - thresholds.max_repetition_ratio) * 1.5)

    # --- OCR fragmentation ----------------------------------------------
    fragmentation = _fragmentation_ratio(text)
    metrics["short_line_ratio"] = round(fragmentation, 4)
    if fragmentation > 0.55:
        issues.append("ocr_fragmentation")
        score -= min(0.3, (fragmentation - 0.55) * 1.2)

    single_char_ratio = sum(1 for w in words if len(w) == 1) / max(1, word_count)
    metrics["single_char_word_ratio"] = round(single_char_ratio, 4)
    if single_char_ratio > 0.25:
        issues.append("ocr_character_scatter")
        score -= min(0.3, (single_char_ratio - 0.25) * 1.5)

    # --- navigation / metadata pollution ---------------------------------
    navigation = _navigation_ratio(text)
    metrics["navigation_ratio"] = round(navigation, 4)
    if navigation > 0.2:
        issues.append("navigation_boilerplate")
        score -= min(0.25, (navigation - 0.2))

    # --- language --------------------------------------------------------
    detected, confidence = detect_language(text)
    declared = normalize_language_code(declared_language)
    metrics["declared_language"] = declared
    if declared and detected != "unknown" and declared != detected:
        issues.append("language_mismatch")
        if thresholds.reject_on_language_mismatch:
            score -= 0.3
        else:
            score -= 0.05

    # --- length, judged against the document's own form ------------------
    short_form = _is_short_form(document_type)
    metrics["short_form_expected"] = short_form
    if not short_form and chars < LONG_FORM_MIN_CHARS:
        # Scaled, not binary. A 15k-character text is mildly suspicious;
        # a 900-character "novel" is a failed download.
        shortfall = 1.0 - (chars / LONG_FORM_MIN_CHARS)
        penalty = min(0.4, shortfall**2 * 0.5)
        if penalty > 0.02:
            issues.append("short_for_declared_form")
            score -= penalty

    # --- format-specific -------------------------------------------------
    extraction_quality = format_metrics.get("extraction_quality")
    if source_format == "pdf" and extraction_quality is not None:
        metrics["pdf_extraction_quality"] = extraction_quality
        if extraction_quality < 0.8:
            issues.append("poor_pdf_text_layer")
            score -= min(0.5, (0.8 - extraction_quality) * 1.5)

    if format_metrics.get("spine_read_failures"):
        issues.append("epub_spine_read_failures")
        score -= 0.05

    score = max(0.0, min(1.0, score))
    metrics["char_count"] = chars
    metrics["word_count"] = word_count

    return QualityReport(
        score=round(score, 4),
        language_detected=detected,
        language_confidence=confidence,
        char_count=chars,
        word_count=word_count,
        issues=tuple(issues),
        metrics=metrics,
    )


def _is_short_form(document_type: str) -> bool:
    low = (document_type or "").lower()
    return any(term in low for term in SHORT_FORM_TYPES)


def _repetition_ratio(text: str) -> float:
    """
    Fraction of non-trivial lines that are duplicates.

    Measured over lines rather than n-grams because the failure mode this
    catches -- a stuck extractor emitting the same page repeatedly, or an
    un-stripped running header -- is line-shaped.
    """
    lines = [line.strip() for line in text.split("\n") if len(line.strip()) > 15]
    if len(lines) < 10:
        return 0.0
    counts = Counter(lines)
    duplicated = sum(count - 1 for count in counts.values() if count > 1)
    return duplicated / len(lines)


def _fragmentation_ratio(text: str) -> float:
    """
    Fraction of lines shorter than 25 characters.

    High values mean hard-wrapped OCR output where every line break is
    arbitrary. Poetry legitimately scores high here, which is why this
    only costs points above a generous 0.55 and never rejects on its own.
    """
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) < 20:
        return 0.0
    short = sum(1 for line in lines if len(line) < 25)
    return short / len(lines)


_NAV_MARKERS = (
    "click here", "next page", "previous page", "table of contents", "back to top",
    "printer-friendly", "share this", "log in", "sign up", "cookie", "javascript",
    "page suivante", "page précédente", "retour au sommaire", "menu principal",
)


def _navigation_ratio(text: str) -> float:
    lines = [line.strip().lower() for line in text.split("\n") if line.strip()]
    if not lines:
        return 0.0
    hits = sum(1 for line in lines if any(marker in line for marker in _NAV_MARKERS))
    return hits / len(lines)


def passes(report: QualityReport, thresholds: QualityThresholds | None = None) -> bool:
    thresholds = thresholds or QualityThresholds()
    if report.score < thresholds.min_score:
        return False
    if "empty_or_negligible_text" in report.issues:
        return False
    return not (thresholds.reject_on_language_mismatch and "language_mismatch" in report.issues)


def entropy(text: str) -> float:
    """
    Shannon entropy per character. Reported for diagnostics only -- an
    unusually low value flags a degenerate extraction (a page of a single
    repeated glyph), but it is not part of the score, because entropy
    varies enormously with language and script for perfectly good text.
    """
    if not text:
        return 0.0
    counts = Counter(text)
    total = len(text)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


__all__ = [
    "ABSOLUTE_MIN_CHARS",
    "LONG_FORM_MIN_CHARS",
    "SHORT_FORM_TYPES",
    "QualityThresholds",
    "entropy",
    "passes",
    "score_quality",
]
