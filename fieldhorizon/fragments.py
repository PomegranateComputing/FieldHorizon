from __future__ import annotations

import re

# Single source of truth for the FIELD_FRAGMENT word-count contract.
# MIN_WORDS mirrors the canon hard gate (evaluate.canon_allowed); MAX_WORDS
# is the upper bound the generation prompt asks for.
MIN_WORDS = 220
MAX_WORDS = 500

_TERMINAL_CHARS = ('.', '!', '?', '"', '”', ')', ']', '}')

_DANGLING_CONJUNCTIONS = re.compile(
    r"\b(?:and|or|but|yet|then|while|because|for|the|of|to|with|from|into|in)\s*$",
    re.IGNORECASE,
)

_BRACKET_PAIRS = {")": "(", "]": "[", "}": "{"}


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def extract_ngrams(text: str, n: int = 3) -> list[str]:
    """
    Lowercased word n-grams, used by the canon motif tracker (review's
    real-upgrade tier §2) to detect statistically overused phrases instead
    of matching against a fixed list of past collapses.
    """
    words = re.findall(r"[a-z0-9']+", text.lower())
    if len(words) < n:
        return []
    return [" ".join(words[i : i + n]) for i in range(len(words) - n + 1)]


def paragraph_count(text: str) -> int:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return len(paras)


def normalize_paragraph(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def dedupe_repeated_paragraphs(text: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    seen: set[str] = set()
    kept: list[str] = []

    for paragraph in paragraphs:
        key = normalize_paragraph(paragraph)
        if key and key not in seen:
            seen.add(key)
            kept.append(paragraph)

    return "\n\n".join(kept).strip()


def extract_field_fragment(text: str) -> str:
    """
    Extract the FIELD_FRAGMENT prose body from a raw model response or a
    saved cycle log. Anchors on the *last* FIELD_FRAGMENT marker so that
    logs containing multiple stamps (initial synthesis, rewrite, final
    selection) yield the most recently written one.
    """
    if "FIELD_FRAGMENT" in text:
        text = text.rsplit("FIELD_FRAGMENT", 1)[1]

    if "METADATA_JSON" in text:
        text = text.split("METADATA_JSON", 1)[0]

    text = text.replace("FIELD_FRAGMENT ENDS.", "")
    text = text.replace("BEGIN PROSE ONLY.", "")
    return dedupe_repeated_paragraphs(text.strip())


def _has_unclosed_brackets(text: str) -> bool:
    stack: list[str] = []
    for ch in text:
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}" and stack and stack[-1] == _BRACKET_PAIRS[ch]:
            stack.pop()
    return bool(stack)


def has_truncation_marker(text: str) -> bool:
    """
    A fragment is treated as complete only when it shows positive signals
    of completion: it ends with terminal punctuation, its double quotes
    are balanced, its brackets are balanced, and it contains no literal
    ellipsis/clip marker. Everything else is suspect: dangling commas,
    semicolons or colons; dangling conjunctions; unclosed brackets; or an
    explicit truncation ellipsis.
    """
    stripped = text.strip()

    if not stripped:
        return True

    if "[...]" in stripped or stripped.endswith("...") or stripped.endswith("…"):
        return True

    if stripped.count('"') % 2 == 1:
        return True

    if _has_unclosed_brackets(stripped):
        return True

    if re.search(r'[,;:]$', stripped):
        return True

    if _DANGLING_CONJUNCTIONS.search(stripped):
        return True

    return not stripped.endswith(_TERMINAL_CHARS)


def fragment_is_usable(text: str) -> bool:
    return word_count(text) >= MIN_WORDS and not has_truncation_marker(text)
