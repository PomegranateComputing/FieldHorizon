"""
Deterministic language detection for the priority languages.

`langdetect` and friends are probabilistic and seed-dependent; the same
text can classify differently between runs unless a seed is pinned. This
subsystem promises reproducibility at equal seed and must run offline in
the unit suite, so detection is done with a fixed scoring function over
function-word frequencies and characteristic character sequences.

Coverage is fr / en / de / es / it / la -- the brief's initial priority
set -- plus `unknown`. That is a deliberate boundary, not an oversight:
returning `unknown` for Polish is honest, whereas forcing it into the
nearest of six is not, and the quality gate treats a language mismatch
against the catalogue's declared language as a real signal.
"""

from __future__ import annotations

import re
from collections import Counter

#: High-frequency function words. Function words are used rather than
#: content words because they are the part of a language that barely
#: changes across genre, register, or century -- a 1650 French treatise
#: and a 1930 French pamphlet share "de", "la", "et" at nearly the same
#: rates.
_PROFILES: dict[str, frozenset[str]] = {
    "en": frozenset(
        ["the", "of", "and", "to", "in", "that", "is", "was", "he", "for", "it", "with", "as", "his", "on", "be", "at", "by", "not", "this", "but", "from", "they", "her", "she", "or", "an", "will", "which", "their", "said", "would", "there", "been", "has", "had", "were", "what", "all", "when", "who", "its"]
    ),
    "fr": frozenset(
        ["de", "la", "le", "et", "les", "des", "en", "un", "une", "du", "dans", "il", "que", "qui", "pour", "est", "au", "ne", "pas", "sur", "se", "plus", "par", "avec", "ce", "son", "tout", "mais", "ils", "elle", "nous", "comme", "sa", "lui", "leur", "ou", "si", "ses", "cette", "aux", "ont", "été", "être"]
    ),
    "de": frozenset(
        ["der", "die", "und", "in", "den", "von", "zu", "das", "mit", "sich", "des", "auf", "für", "ist", "nicht", "ein", "eine", "als", "auch", "es", "an", "werden", "aus", "er", "hat", "dass", "sie", "nach", "bei", "um", "noch", "wie", "über", "nur", "oder", "aber", "vor", "durch", "man"]
    ),
    "es": frozenset(
        ["de", "la", "que", "el", "en", "y", "a", "los", "del", "se", "las", "por", "un", "para", "con", "no", "una", "su", "al", "lo", "como", "más", "pero", "sus", "le", "ya", "o", "este", "sí", "porque", "esta", "entre", "cuando", "muy", "sin", "sobre", "también", "me", "hasta", "hay"]
    ),
    "it": frozenset(
        ["di", "che", "e", "il", "la", "in", "un", "a", "per", "non", "sono", "mi", "si", "ma", "con", "come", "da", "le", "si", "più", "questo", "lo", "ha", "se", "io", "ci", "gli", "nel", "alla", "anche", "della", "sua", "o", "quando", "molto", "essere", "loro", "cosa", "sono"]
    ),
    "la": frozenset(
        ["et", "in", "est", "non", "ad", "ut", "cum", "quod", "qui", "si", "sed", "ex", "de", "per", "te", "me", "se", "nec", "quae", "quam", "esse", "enim", "atque", "autem", "hoc", "eius", "sunt", "etiam", "ipse", "tamen", "omnia", "aut", "quo", "tunc", "erat", "sicut"]
    ),
}

#: Character sequences that are strong positive evidence for one language
#: and rare in the others. These break ties that function words alone
#: cannot -- notably Latin vs Italian, which share a great deal.
_CHAR_SIGNALS: dict[str, tuple[tuple[str, float], ...]] = {
    "de": (("sch", 1.5), ("ung", 1.2), ("ß", 3.0), ("ei", 0.4), ("ch", 0.4), ("keit", 2.0)),
    "fr": (("qu", 0.5), ("eux", 1.2), ("aient", 2.0), ("è", 1.5), ("ê", 1.5), ("ç", 2.0), ("ité", 1.5)),
    "es": (("ción", 2.5), ("ñ", 3.0), ("ll", 0.5), ("dad", 1.2), ("¿", 3.0), ("¡", 3.0)),
    "it": (("zione", 2.5), ("gli", 2.0), ("cch", 1.5), ("tà", 2.0), ("ggi", 1.5)),
    "la": (("ibus", 2.5), ("orum", 2.5), ("arum", 2.5), ("tur", 1.2), ("que ", 0.8)),
    "en": (("th", 0.5), ("ing", 1.0), ("tion", 0.6), ("ough", 1.5)),
}

_WORD_RE = re.compile(r"[a-zà-öø-ÿœæ]+", re.IGNORECASE)

#: Below this many words, a verdict is not meaningful. Short manifestos
#: and declarations are legitimate corpus members, so this is low -- but
#: a 12-word fragment genuinely cannot be attributed to a language with
#: any confidence, and pretending otherwise would corrupt the quality
#: signal that depends on it.
MIN_WORDS_FOR_DETECTION = 20


def detect_language(text: str, sample_chars: int = 20000) -> tuple[str, float]:
    """
    Returns (iso_639_1_code, confidence in 0..1).

    Confidence is the winning score's margin over the runner-up,
    normalized -- so "clearly French" and "French or maybe Italian" are
    distinguishable, which is what the quality gate needs.
    """
    sample = text[:sample_chars].lower()
    words = _WORD_RE.findall(sample)
    if len(words) < MIN_WORDS_FOR_DETECTION:
        return "unknown", 0.0

    counts = Counter(words)
    total = sum(counts.values())

    scores: dict[str, float] = {}
    for lang, profile in _PROFILES.items():
        hits = sum(count for word, count in counts.items() if word in profile)
        score = hits / total
        for sequence, weight in _CHAR_SIGNALS.get(lang, ()):
            occurrences = sample.count(sequence)
            if occurrences:
                score += weight * min(0.05, occurrences / max(1, len(sample)) * 10)
        scores[lang] = score

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0.0

    # An absolute floor as well as a relative one: a text of proper nouns
    # matches nobody's function words, and should come back `unknown`
    # rather than as whichever profile happened to score 0.02.
    if best_score < 0.04:
        return "unknown", 0.0

    margin = (best_score - runner_up_score) / best_score if best_score > 0 else 0.0
    confidence = max(0.0, min(1.0, 0.45 + margin * 0.55))
    return best, round(confidence, 3)


#: ISO 639-2/B -> 639-1, for the several sources that emit three-letter
#: codes (Gallica and OAI-PMH Dublin Core in particular).
_ISO_639_2_TO_1 = {
    "eng": "en", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de",
    "spa": "es", "ita": "it", "lat": "la", "dut": "nl", "nld": "nl",
    "por": "pt", "rus": "ru", "gre": "el", "ell": "el", "ara": "ar",
    "heb": "he", "chi": "zh", "zho": "zh", "jpn": "ja", "pol": "pl",
    "swe": "sv", "dan": "da", "nor": "no", "fin": "fi", "cze": "cs",
    "ces": "cs", "hun": "hu", "tur": "tr", "ukr": "uk", "ron": "ro", "rum": "ro",
}


def normalize_language_code(code: str) -> str:
    """
    Normalize whatever a catalogue emitted to a bare ISO 639-1 code.

    Handles `en-GB` (-> `en`), `fre` (-> `fr`), `EN` (-> `en`), and the
    various URI forms Europeana uses. Anything unrecognized is returned
    lowercased rather than discarded -- an unusual but real code is more
    useful than an empty string.
    """
    code = (code or "").strip().lower()
    if not code:
        return ""
    if "/" in code:
        code = code.rsplit("/", 1)[-1]
    code = code.replace("_", "-").split("-")[0]
    if len(code) == 3:
        return _ISO_639_2_TO_1.get(code, code)
    return code


SUPPORTED_LANGUAGES = frozenset(_PROFILES)


__all__ = ["MIN_WORDS_FOR_DETECTION", "SUPPORTED_LANGUAGES", "detect_language", "normalize_language_code"]
