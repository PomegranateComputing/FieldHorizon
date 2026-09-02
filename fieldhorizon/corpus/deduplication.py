"""
Deduplication across works, editions, and files.

A ladder of five increasingly fuzzy tests, run in order and stopping at
the first match, so the *reason* two documents were merged is always
recorded and always the strongest available one.

The hard part of corpus deduplication is not finding duplicates; it is
**not merging things that merely look alike**. The following are never
duplicates of each other, and each has an explicit guard here:

* two translations of the same work (Baudelaire's Poe is not Poe);
* an original and an annotated or critical edition;
* a complete text and a clearly-labelled extract;
* a corrected transcription and the flawed one it supersedes -- these are
  related, and the relationship is recorded, but the better one wins
  rather than both being silently collapsed.

Exact duplicates keep one canonical blob and *all* provenance rows: two
sources offering byte-identical files is a fact worth keeping, and the
attribution obligations of both may differ.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

#: SimHash width. 64 bits is the standard choice and gives a Hamming
#: distance whose thresholds are well understood.
SIMHASH_BITS = 64

#: Shingle size for MinHash/SimHash tokenization, in words. Three is
#: small enough to survive light editorial variation and large enough
#: that common phrases do not dominate.
SHINGLE_SIZE = 3

#: Hamming distance at or below which two SimHashes are "near duplicate".
#: 3/64 is conservative -- it catches reformatting and light OCR variance
#: without merging different editions.
NEAR_DUPLICATE_MAX_DISTANCE = 3


class DuplicateKind(StrEnum):
    EXACT_RAW = "exact_raw"
    EXACT_NORMALIZED = "exact_normalized"
    IDENTIFIER = "identifier"
    TITLE_AUTHOR = "title_author"
    NEAR = "near"


@dataclass(frozen=True)
class DuplicateVerdict:
    is_duplicate: bool
    kind: DuplicateKind | None = None
    matched_document_id: str = ""
    similarity: float = 0.0
    reason: str = ""
    #: Set when two documents are *related* but deliberately kept
    #: separate -- different translations, different editions. Recorded so
    #: the relationship is not lost just because the merge was refused.
    related_but_distinct: bool = False


# --------------------------------------------------------------------------
# Normalization of comparison keys
# --------------------------------------------------------------------------

# Leading articles, including the French contracted forms (du/de/des/d')
# that library catalogues drop as a matter of course. Without them
# "Du contrat social" and "Contrat social" never match, which is the same
# house-style disagreement the English "The" handles.
_ARTICLE_PREFIX = re.compile(
    r"^(the|a|an|le|la|les|l'|d'|du|de|des|un|une|der|die|das|den|dem|ein|eine|"
    r"el|los|las|lo|il|gli|i)\s+",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

#: Subtitle markers that carry edition information rather than work
#: identity. Stripped for work-level comparison, preserved for edition.
_SUBTITLE_SPLIT = re.compile(r"\s*[:;]\s+|\s+--\s+|\s+—\s+")


def normalize_title(title: str, *, strip_subtitle: bool = True) -> str:
    """
    Title reduced to a comparison key: accents folded, punctuation
    dropped, leading article removed, whitespace collapsed.

    Article stripping matters more than it looks: "The Republic" and
    "Republic" are the same work in two catalogues' house styles, and
    without this they never match.
    """
    text = unicodedata.normalize("NFKD", title or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower().strip()
    if strip_subtitle:
        text = _SUBTITLE_SPLIT.split(text)[0]
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    text = _ARTICLE_PREFIX.sub("", text).strip()
    return text


def normalize_author(author: str) -> str:
    """
    Author key that survives "Surname, Forename" vs "Forename Surname"
    and dates in parentheses -- the two variations every library
    catalogue disagrees about.
    """
    text = unicodedata.normalize("NFKD", author or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"\b\d{3,4}\s*-\s*\d{0,4}\b", " ", text)  # life dates
    text = re.sub(r"\((.*?)\)", " ", text)
    text = _PUNCT_RE.sub(" ", text)
    tokens = sorted(t for t in _WS_RE.split(text) if len(t) > 1)
    return " ".join(tokens)


def work_key(title: str, authors: list[str] | tuple[str, ...], language: str) -> str:
    """
    The work-identity key. Language is part of it *by design*: a French
    translation and the English original are different documents with
    different rights, different quality, and different retrieval value,
    and merging them would be the single most damaging dedup error this
    corpus could make.
    """
    author_key = "|".join(sorted(normalize_author(a) for a in (authors or []) if a))
    return f"{normalize_title(title)}::{author_key}::{(language or '').lower()}"


# --------------------------------------------------------------------------
# Identifier extraction
# --------------------------------------------------------------------------

_IDENTIFIER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("isbn", re.compile(r"\b(?:isbn[:\s]*)?((?:97[89][-\s]?)?\d{1,5}[-\s]?\d{1,7}[-\s]?\d{1,7}[-\s]?[\dxX])\b")),
    ("doi", re.compile(r"\b(10\.\d{4,9}/[-._;()/:a-z0-9]+)\b", re.IGNORECASE)),
    ("ark", re.compile(r"\b(ark:/\d+/[a-z0-9]+)\b", re.IGNORECASE)),
    ("gutenberg", re.compile(r"gutenberg\.org/(?:ebooks|files|cache/epub)/(\d+)", re.IGNORECASE)),
    ("oclc", re.compile(r"\boclc[:\s]*(\d{5,12})\b", re.IGNORECASE)),
    ("lccn", re.compile(r"\blccn[:\s]*([a-z]{0,3}\d{8,10})\b", re.IGNORECASE)),
]


def extract_identifiers(*values: str) -> set[str]:
    """
    Pull normalized `scheme:value` identifiers out of arbitrary metadata
    strings. Identifier equality is the strongest non-byte-level signal
    available: two records sharing a DOI or an ARK are the same edition.
    """
    found: set[str] = set()
    for value in values:
        if not value:
            continue
        for scheme, pattern in _IDENTIFIER_PATTERNS:
            for match in pattern.finditer(value):
                raw = match.group(1)
                normalized = re.sub(r"[-\s]", "", raw).lower()
                if scheme == "isbn" and len(normalized) not in (10, 13):
                    continue
                # An ARK already carries its own "ark:" prefix in the
                # captured value; re-prefixing produced "ark:ark:/...",
                # which would never match the same ARK written any other
                # way.
                if normalized.startswith(f"{scheme}:"):
                    found.add(normalized)
                else:
                    found.add(f"{scheme}:{normalized}")
    return found


# --------------------------------------------------------------------------
# SimHash
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def shingles(text: str, size: int = SHINGLE_SIZE, limit: int = 200_000) -> list[str]:
    tokens = [t.lower() for t in _TOKEN_RE.findall(text[:limit])]
    if len(tokens) < size:
        return [" ".join(tokens)] if tokens else []
    return [" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)]


def _hash64(value: str) -> int:
    """
    blake2b truncated to 64 bits. Chosen over Python's built-in `hash()`
    because that is randomized per process (PYTHONHASHSEED) -- a SimHash
    stored in the database must mean the same thing tomorrow as it did
    today, which `hash()` cannot promise.
    """
    return int.from_bytes(hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest(), "big")


def simhash(text: str, bits: int = SIMHASH_BITS) -> int:
    """
    Charikar SimHash over word shingles. Similar documents produce
    SimHashes at small Hamming distance, so near-duplicate detection is a
    popcount rather than a pairwise text comparison.
    """
    features = shingles(text)
    if not features:
        return 0
    vector = [0] * bits
    for feature in features:
        digest = _hash64(feature)
        for bit in range(bits):
            vector[bit] += 1 if (digest >> bit) & 1 else -1
    result = 0
    for bit in range(bits):
        if vector[bit] > 0:
            result |= 1 << bit
    return result


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def simhash_similarity(a: int, b: int, bits: int = SIMHASH_BITS) -> float:
    return 1.0 - (hamming_distance(a, b) / bits)


def minhash_signature(text: str, num_hashes: int = 64) -> tuple[int, ...]:
    """
    MinHash signature, for Jaccard estimation where SimHash's sensitivity
    to length is a problem (comparing a short manifesto against a long
    collection that contains it).
    """
    features = set(shingles(text))
    if not features:
        return tuple([0] * num_hashes)
    signature: list[int] = []
    for seed in range(num_hashes):
        minimum = min(
            int.from_bytes(
                hashlib.blake2b(feature.encode("utf-8"), digest_size=8, salt=seed.to_bytes(2, "big")).digest(),
                "big",
            )
            for feature in features
        )
        signature.append(minimum)
    return tuple(signature)


def jaccard_estimate(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)


# --------------------------------------------------------------------------
# The distinctness guards
# --------------------------------------------------------------------------

_EDITION_MARKERS = (
    "annotated", "annoté", "critical edition", "édition critique", "illustrated",
    "illustré", "abridged", "abrégé", "revised", "revu", "expurgated", "kommentiert",
    "with notes", "avec notes", "edited by", "édité par", "second edition",
    "deuxième édition", "new edition", "nouvelle édition",
)

_EXCERPT_MARKERS = (
    "selections from", "extraits de", "excerpt", "extrait", "selected", "choisis",
    "an anthology of", "chapters from", "chapitres de", "auszug",
)


@dataclass
class DocumentFingerprint:
    """Everything the deduplicator needs about one document."""

    document_id: str
    raw_sha256: str = ""
    normalized_sha256: str = ""
    simhash_value: int = 0
    title: str = ""
    authors: tuple[str, ...] = ()
    language: str = ""
    translator: str = ""
    identifiers: set[str] = field(default_factory=set)
    char_count: int = 0
    quality_score: float = 0.0
    edition_id: str = ""


def _translations_differ(a: DocumentFingerprint, b: DocumentFingerprint) -> bool:
    if a.language and b.language and a.language != b.language:
        return True
    translator_a = normalize_author(a.translator)
    translator_b = normalize_author(b.translator)
    return bool(translator_a and translator_b and translator_a != translator_b)


def _editions_differ(a: DocumentFingerprint, b: DocumentFingerprint) -> bool:
    if a.edition_id and b.edition_id and a.edition_id != b.edition_id:
        return True
    title_a = (a.title or "").lower()
    title_b = (b.title or "").lower()
    marked_a = any(marker in title_a for marker in _EDITION_MARKERS)
    marked_b = any(marker in title_b for marker in _EDITION_MARKERS)
    return marked_a != marked_b


def _is_excerpt_pair(a: DocumentFingerprint, b: DocumentFingerprint) -> bool:
    """
    One is a labelled extract of the other, or one is drastically shorter.
    A 4x length difference between two texts claiming the same title is
    an anthology-versus-complete-work pair, not a duplicate.
    """
    title_a = (a.title or "").lower()
    title_b = (b.title or "").lower()
    if any(m in title_a for m in _EXCERPT_MARKERS) != any(m in title_b for m in _EXCERPT_MARKERS):
        return True
    if a.char_count and b.char_count:
        longer, shorter = max(a.char_count, b.char_count), min(a.char_count, b.char_count)
        if shorter and longer / shorter >= 4.0:
            return True
    return False


def compare(new: DocumentFingerprint, existing: DocumentFingerprint) -> DuplicateVerdict:
    """
    Run the ladder against one existing document. First match wins.

    Byte equality is checked before anything else and is *unconditional*:
    if two files are identical, no amount of differing metadata makes them
    different documents -- one of the catalogues is simply wrong about
    which translation it is serving, and the bytes are the ground truth.
    """
    # 1 & 2 -- byte equality.
    if new.raw_sha256 and new.raw_sha256 == existing.raw_sha256:
        return DuplicateVerdict(
            True, DuplicateKind.EXACT_RAW, existing.document_id, 1.0,
            "identical raw file (sha256)",
        )
    if new.normalized_sha256 and new.normalized_sha256 == existing.normalized_sha256:
        return DuplicateVerdict(
            True, DuplicateKind.EXACT_NORMALIZED, existing.document_id, 1.0,
            "identical normalized text (sha256)",
        )

    # From here on, the distinctness guards apply: a shared identifier or
    # near-identical text between two different translations is expected,
    # not evidence of duplication.
    if _translations_differ(new, existing):
        return DuplicateVerdict(
            False, None, existing.document_id, 0.0,
            "different language or translator -- distinct translations are never merged",
            related_but_distinct=True,
        )

    # 3 -- shared strong identifier.
    shared = new.identifiers & existing.identifiers
    if shared:
        if _editions_differ(new, existing):
            return DuplicateVerdict(
                False, None, existing.document_id, 0.0,
                f"shares {sorted(shared)[0]} but the editions differ", related_but_distinct=True,
            )
        return DuplicateVerdict(
            True, DuplicateKind.IDENTIFIER, existing.document_id, 1.0,
            f"shared identifier {sorted(shared)[0]}",
        )

    # 4 -- normalized title + author + language.
    if new.title and existing.title and work_key(new.title, list(new.authors), new.language) == work_key(
        existing.title, list(existing.authors), existing.language
    ):
        if _editions_differ(new, existing):
            return DuplicateVerdict(
                False, None, existing.document_id, 0.0,
                "same work, substantively different editions", related_but_distinct=True,
            )
        if _is_excerpt_pair(new, existing):
            return DuplicateVerdict(
                False, None, existing.document_id, 0.0,
                "same work, but one is an extract of the other", related_but_distinct=True,
            )
        return DuplicateVerdict(
            True, DuplicateKind.TITLE_AUTHOR, existing.document_id, 0.95,
            "identical normalized title, author, and language",
        )

    # 5 -- near-duplicate text.
    if new.simhash_value and existing.simhash_value:
        distance = hamming_distance(new.simhash_value, existing.simhash_value)
        if distance <= NEAR_DUPLICATE_MAX_DISTANCE:
            similarity = simhash_similarity(new.simhash_value, existing.simhash_value)
            if _editions_differ(new, existing) or _is_excerpt_pair(new, existing):
                return DuplicateVerdict(
                    False, None, existing.document_id, similarity,
                    "near-identical text but a distinct edition or extract", related_but_distinct=True,
                )
            return DuplicateVerdict(
                True, DuplicateKind.NEAR, existing.document_id, similarity,
                f"near-duplicate text (SimHash distance {distance})",
            )

    return DuplicateVerdict(False)


def find_duplicate(new: DocumentFingerprint, corpus: list[DocumentFingerprint]) -> DuplicateVerdict:
    """
    Compare against the whole existing corpus, returning the strongest
    match found. Exact matches short-circuit; otherwise the best-scoring
    duplicate verdict wins, and if none is a duplicate the most
    informative "related but distinct" verdict is returned so the
    relationship can still be recorded.
    """
    best: DuplicateVerdict | None = None
    related: DuplicateVerdict | None = None

    for existing in corpus:
        verdict = compare(new, existing)
        if verdict.is_duplicate:
            if verdict.kind in (DuplicateKind.EXACT_RAW, DuplicateKind.EXACT_NORMALIZED):
                return verdict
            if best is None or verdict.similarity > best.similarity:
                best = verdict
        elif verdict.related_but_distinct and related is None:
            related = verdict

    return best or related or DuplicateVerdict(False)


def choose_representative(candidates: list[DocumentFingerprint]) -> str:
    """
    Pick the canonical materialization for a duplicate cluster.

    Highest quality score wins; length breaks ties (a complete text beats
    a truncated one); document_id breaks the remaining ties so the choice
    is reproducible across runs rather than dependent on iteration order.
    """
    if not candidates:
        return ""
    ranked = sorted(candidates, key=lambda f: (f.quality_score, f.char_count, f.document_id), reverse=True)
    return ranked[0].document_id


def cluster_id_for(document_ids: list[str]) -> str:
    """Stable cluster id from its members, independent of discovery order."""
    joined = "|".join(sorted(document_ids))
    return "dup_" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "NEAR_DUPLICATE_MAX_DISTANCE",
    "SIMHASH_BITS",
    "DocumentFingerprint",
    "DuplicateKind",
    "DuplicateVerdict",
    "choose_representative",
    "cluster_id_for",
    "compare",
    "extract_identifiers",
    "find_duplicate",
    "hamming_distance",
    "jaccard_estimate",
    "minhash_signature",
    "normalize_author",
    "normalize_title",
    "simhash",
    "simhash_similarity",
    "work_key",
]
