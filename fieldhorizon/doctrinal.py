from __future__ import annotations

import logging
import re

from .config import AppConfig
from .embeddings import cosine_similarity, current_embedding_version
from .fragments import extract_field_fragment, word_count
from .llm import embed as embed_text
from .ontology_spec import get_ontology
from .storage_vectors import VectorStore

logger = logging.getLogger(__name__)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text.strip()) if s.strip()]


def axiom_category_from_row(row: dict) -> str:
    return str(row.get("category", "")).lower()


def score_doctrinal_enforcement_keyword(response: str, json_rows: list[dict]) -> float:
    """
    Fast, network-free fallback (review's real-upgrade tier §3): counts an
    axiom as "enforced" if any of its ontology-declared keywords (or, absent
    those, a handful of long words pulled from its own statement) appear in
    the fragment. Used automatically when the embedding model is
    unreachable, and always available for offline tests.
    """
    if not json_rows:
        return 1.0

    frag = extract_field_fragment(response)
    low = frag.lower()
    hits = 0

    ontology = get_ontology()

    for row in json_rows:
        category = axiom_category_from_row(row)
        keywords = ontology.keywords_for(category)

        if not keywords:
            statement = str(row.get("statement", "")).lower()
            keywords = tuple(w for w in re.findall(r"[a-zA-Z_]+", statement) if len(w) >= 6)[:5]

        if any(re.search(rf"\b{re.escape(k)}\b", low) for k in keywords):
            hits += 1

    return hits / len(json_rows)


def get_axiom_vector(cfg: AppConfig, axiom_id: str, statement: str) -> list[float]:
    """
    Cached per (axiom id, embedding_version): a json_entries row's
    statement is static between cycles, so it is embedded once and reused,
    unlike a fragment's sentences which are new on every evaluation. Keyed
    on embedding_version (model name + revision), not just model name, so
    a re-pulled model under the same tag re-embeds rather than silently
    reusing a vector from different weights (standing rule). Goes through
    the same storage_vectors seam as embeddings.backfill_axiom_embeddings,
    so a proactive batch backfill and this lazy on-demand path never
    disagree about where an axiom's vector lives.
    """
    version = current_embedding_version(cfg)
    store = VectorStore(cfg, "axiom")

    vector = store.get(axiom_id, version)
    if vector is not None:
        return vector

    vector = embed_text(cfg, statement)
    store.upsert(axiom_id, vector, version)
    return vector


def score_doctrinal_enforcement_embedding(cfg: AppConfig, response: str, json_rows: list[dict]) -> float:
    """
    Each axiom's score is the max cosine similarity between its statement
    embedding and any of the fragment's sentence embeddings (review's
    real-upgrade tier §3); doctrinal_enforcement is the mean over axioms.
    Unlike keyword matching, this can't be gamed by stuffing the literal
    category term into the prose -- it rewards a sentence that actually
    develops the axiom's meaning.
    """
    if not json_rows:
        return 1.0

    frag = extract_field_fragment(response)
    sentences = split_sentences(frag)
    if not sentences:
        raise ValueError("no sentences available to embed")

    sentence_vectors = [embed_text(cfg, sentence) for sentence in sentences]

    scores: list[float] = []
    for row in json_rows:
        statement = str(row.get("statement", "")).strip()
        if not statement:
            continue
        axiom_id = str(row.get("id", statement))
        axiom_vector = get_axiom_vector(cfg, axiom_id, statement)
        scores.append(max(cosine_similarity(axiom_vector, sv) for sv in sentence_vectors))

    if not scores:
        return 1.0

    return sum(scores) / len(scores)


def score_doctrinal_enforcement(
    cfg: AppConfig | None,
    response: str,
    json_rows: list[dict],
) -> tuple[float, str]:
    """
    Returns (score, method). Tries the embedding method first and falls
    back to the keyword method -- automatically, and logged -- whenever
    `cfg` is absent or the embedding model can't be reached (review's
    real-upgrade tier §3's "selected automatically and logged" requirement).
    """
    if cfg is not None:
        try:
            return score_doctrinal_enforcement_embedding(cfg, response, json_rows), "embedding"
        except Exception as exc:
            logger.warning(
                "Embedding-based doctrinal enforcement unavailable, "
                "falling back to keyword scoring: %s",
                exc,
            )

    return score_doctrinal_enforcement_keyword(response, json_rows), "keyword"


# Kept in sync with evaluate.SYMBOLIC_TERMS -- see score_symbolic_density_keyword.
SYMBOLIC_TERMS = [
    "judgment", "logos", "tawhid", "mercy", "idolatry", "apocalypse",
    "revelation", "sacrifice", "machine", "audit", "confession",
    "transcendence", "idol", "collapse", "canon", "heresy",
    "obedience", "censorship", "merit", "institution", "decay",
]

# Calibrated against a known-CANON fragment (9/9 sentences over threshold)
# vs. generic dark-AI filler (1/3 sentences over threshold) -- see the
# real-upgrade-tier follow-up commit for the live calibration.
SYMBOLIC_SIMILARITY_THRESHOLD = 0.55

_symbolic_term_vector_cache: dict[tuple[str, str], list[float]] = {}


def _symbolic_term_vector(cfg: AppConfig, term: str) -> list[float]:
    """
    In-process cache only -- these 21 terms are code constants, not user
    data, so there's no need for a DB-backed cache like get_axiom_vector's;
    they're embedded once per process and reused for every evaluation.
    Keyed on embedding_version (not just model name) for the same reason
    as get_axiom_vector: a re-pulled model under the same tag must not
    silently reuse a cached vector from different weights.
    """
    key = (current_embedding_version(cfg), term)
    if key not in _symbolic_term_vector_cache:
        _symbolic_term_vector_cache[key] = embed_text(cfg, term)
    return _symbolic_term_vector_cache[key]


def score_symbolic_density_keyword(response: str) -> float:
    """
    Fast, network-free fallback: literal substring counting against a
    fixed term list. This is exactly the method the review flagged as
    Goodhart bait alongside the old doctrinal_enforcement -- the rewriter
    prompt instructs the model to "use exact category terms," so counting
    substrings counts what the model was already taught to stuff.
    """
    frag = extract_field_fragment(response)
    low = frag.lower()
    count = sum(low.count(term) for term in SYMBOLIC_TERMS)
    words = max(1, word_count(low))
    return min(1.0, (count / words) * 20)


def score_symbolic_density_embedding(cfg: AppConfig, response: str) -> float:
    """
    Embedding replacement for the keyword count (review §8): a sentence
    counts as symbolically developed only if its meaning is close to one
    of SYMBOLIC_TERMS' embeddings, not if it contains the literal
    substring -- repeating a term verbatim doesn't inflate this the way
    it inflates a substring count, since cosine similarity to the term
    saturates rather than accumulating per repetition.
    """
    frag = extract_field_fragment(response)
    sentences = split_sentences(frag)
    if not sentences:
        raise ValueError("no sentences available to embed")

    sentence_vectors = [embed_text(cfg, sentence) for sentence in sentences]
    term_vectors = [_symbolic_term_vector(cfg, term) for term in SYMBOLIC_TERMS]

    hits = sum(
        1
        for sv in sentence_vectors
        if max(cosine_similarity(sv, tv) for tv in term_vectors) >= SYMBOLIC_SIMILARITY_THRESHOLD
    )

    return min(1.0, hits / len(sentences))


def score_symbolic_density(cfg: AppConfig | None, response: str) -> tuple[float, str]:
    """
    Returns (score, method), mirroring score_doctrinal_enforcement's
    automatic-and-logged fallback selection.
    """
    if cfg is not None:
        try:
            return score_symbolic_density_embedding(cfg, response), "embedding"
        except Exception as exc:
            logger.warning(
                "Embedding-based symbolic density unavailable, falling back to keyword scoring: %s",
                exc,
            )

    return score_symbolic_density_keyword(response), "keyword"
