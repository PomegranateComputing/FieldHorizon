"""
The autonomous curator: which admissible documents to acquire next.

The first and most important thing this module does is **not** score
legality. Rights compliance is an absolute filter applied before any
candidate reaches here (rule: "la conformité juridique est un filtre
absolu, jamais un simple score"). A rejected or quarantined document has
no score, because scoring it would imply it could be outweighed.

Everything below therefore operates on an already-admissible pool and
answers one question: given a budget, which of these does Field Horizon
most need?

The scoring is weighted-sum with configurable weights, and it is
explicitly *anti-monoculture*. Left alone, any relevance score over
Western library catalogues converges on the same three hundred famous
English books. Three mechanisms fight that:

* **Diversity terms score the corpus, not the document.** A French text
  is worth more when the corpus is 90% English -- the same document's
  score changes as the corpus fills.
* **Soft quotas** (`quota_penalty`) progressively suppress a dimension
  that is over-represented, rather than hard-blocking it.
* **A popularity discount** on the handful of universally-anthologized
  works, so that the fifteenth edition of the same canonical title loses
  to something the corpus has never seen.

Reproducibility: selection is deterministic given the same seed, corpus
state, and candidate pool. Exploration is controlled by a seeded PRNG,
never by `random` module state.
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
from collections import Counter
from dataclasses import dataclass, field

from .models import Candidate

logger = logging.getLogger(__name__)


@dataclass
class SelectionWeights:
    """
    Relative weights, not a probability distribution -- they need not sum
    to 1. Defaults are tuned for the `broad` profile.
    """

    license_certainty: float = 0.16
    source_confidence: float = 0.10
    expected_quality: float = 0.10
    metadata_completeness: float = 0.06
    novelty: float = 0.12
    language_diversity: float = 0.10
    geographic_diversity: float = 0.05
    chronological_diversity: float = 0.05
    author_diversity: float = 0.05
    tradition_diversity: float = 0.05
    thematic_relevance: float = 0.14
    canonical_value: float = 0.06
    download_cost: float = -0.05
    duplication_risk: float = -0.10
    ocr_risk: float = -0.06

    @classmethod
    def from_dict(cls, data: dict | None) -> SelectionWeights:
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: float(v) for k, v in data.items() if k in known})


# --------------------------------------------------------------------------
# Field Horizon's thematic profile
# --------------------------------------------------------------------------

#: Subject terms Field Horizon actively wants, grouped by tradition so
#: that tradition diversity can be measured rather than assumed. The
#: groups are intentionally not all European.
THEMATIC_PROFILE: dict[str, tuple[str, ...]] = {
    "philosophy": (
        "philosophy", "philosophie", "philosophie", "filosofia", "metaphysics", "métaphysique",
        "ontology", "epistemology", "ethics", "éthique", "logic", "dialectic", "phenomenology",
        "stoicism", "scepticism", "idealism", "materialism", "existentialism",
    ),
    "religion": (
        "religion", "theology", "théologie", "scripture", "sacred", "sacré", "bible", "quran",
        "koran", "talmud", "vedas", "upanishads", "sutra", "tao", "confucian", "buddhism",
        "hinduism", "islam", "christianity", "judaism", "mysticism", "mystique", "gnostic",
        "heresy", "hérésie", "apocalypse", "eschatology", "prophecy", "liturgy", "patristics",
    ),
    "mythology": (
        "mythology", "mythologie", "myth", "folklore", "legend", "légende", "saga", "epic",
        "épopée", "fairy tales", "contes", "oral tradition", "cosmogony",
    ),
    "political_theory": (
        "political science", "science politique", "politics", "political theory", "government",
        "state", "sovereignty", "constitution", "constitutional", "law", "droit", "jurisprudence",
        "natural law", "social contract", "republicanism", "monarchy", "anarchism", "socialism",
        "communism", "fascism", "liberalism", "conservatism", "revolution", "utopia", "dystopia",
        "propaganda", "manifesto", "declaration", "charter",
    ),
    "history_of_ideas": (
        "intellectual history", "history of ideas", "histoire des idées", "historiography",
        "renaissance", "enlightenment", "lumières", "reformation", "scholasticism", "humanism",
    ),
    "human_sciences": (
        "psychology", "psychologie", "anthropology", "anthropologie", "sociology", "sociologie",
        "ethnography", "ethnologie", "social criticism", "critique sociale", "political economy",
    ),
    "systems": (
        "cybernetics", "cybernétique", "systems theory", "information theory", "logic",
        "mathematics", "epistemology", "philosophy of science", "scientific method",
        "natural philosophy", "astronomy", "physics", "biology", "evolution",
    ),
    "literature": (
        "literature", "littérature", "fiction", "poetry", "poésie", "drama", "théâtre",
        "tragedy", "satire", "allegory", "essays", "essais", "letters", "correspondence",
    ),
    "history": (
        "history", "histoire", "geschichte", "storia", "historia", "chronicle", "annals",
        "biography", "memoirs", "travel", "voyages", "exploration", "war", "civilization",
    ),
    "rhetoric": (
        "speeches", "discours", "orations", "sermons", "homilies", "addresses", "pamphlets",
        "tracts", "polemics", "apologetics",
    ),
}

_FLAT_THEMES: dict[str, str] = {
    term.lower(): tradition for tradition, terms in THEMATIC_PROFILE.items() for term in terms
}

#: Priority languages. Others are accepted -- the pipeline handles them --
#: but score lower on the language-coverage term until the priorities are
#: represented.
PRIORITY_LANGUAGES = ("fr", "en", "de", "es", "it", "la")

#: Works so universally anthologized that a corpus acquires several
#: editions of them by accident. Discounted, never excluded.
_OVERREPRESENTED_TITLES = (
    "bible", "shakespeare", "alice in wonderland", "pride and prejudice", "sherlock holmes",
    "moby dick", "frankenstein", "dracula", "the art of war", "the prince", "the republic",
    "don quixote", "les misérables", "war and peace", "iliad", "odyssey", "divine comedy",
)


@dataclass
class CorpusProfile:
    """
    What the corpus already contains. Recomputed before each selection
    pass; the diversity terms are all relative to this.
    """

    language_counts: Counter = field(default_factory=Counter)
    source_counts: Counter = field(default_factory=Counter)
    century_counts: Counter = field(default_factory=Counter)
    author_counts: Counter = field(default_factory=Counter)
    tradition_counts: Counter = field(default_factory=Counter)
    work_keys: set[str] = field(default_factory=set)
    total: int = 0

    @classmethod
    def from_items(cls, items) -> CorpusProfile:
        profile = cls()
        for item in items:
            profile.total += 1
            profile.language_counts[(item.language or "unknown").lower()] += 1
            profile.source_counts[item.source_id] += 1
            century = _century_of(item.row.get("publication_date") or "")
            if century:
                profile.century_counts[century] += 1
            for author in item.authors or []:
                profile.author_counts[_author_key(author)] += 1
            for tradition in _traditions_of(item.row.get("subjects") or "[]"):
                profile.tradition_counts[tradition] += 1
            if item.canonical_work_id:
                profile.work_keys.add(item.canonical_work_id)
        return profile


def _century_of(date_text: str) -> str:
    import re

    match = re.search(r"\b(1[0-9]{3}|20[0-2][0-9])\b", date_text or "")
    if not match:
        return ""
    return f"{int(match.group(1)) // 100 + 1}"


def _author_key(author: str) -> str:
    from .deduplication import normalize_author

    return normalize_author(author)


def _traditions_of(subjects_json: str) -> list[str]:
    import json

    try:
        subjects = json.loads(subjects_json) if isinstance(subjects_json, str) else list(subjects_json or [])
    except (TypeError, ValueError):
        subjects = []
    return _traditions_from_subjects([str(s) for s in subjects])


def _traditions_from_subjects(subjects: list[str] | tuple[str, ...]) -> list[str]:
    found: list[str] = []
    blob = " | ".join(subjects).lower()
    for term, tradition in _FLAT_THEMES.items():
        if term in blob and tradition not in found:
            found.append(tradition)
    return found


# --------------------------------------------------------------------------
# Component scores
# --------------------------------------------------------------------------


def _diversity_bonus(counts: Counter, key: str, total: int) -> float:
    """
    How much a new item of category `key` improves coverage.

    1.0 for a category the corpus has never seen, falling toward 0 as its
    share grows. The square-root shape is deliberate: it keeps a strong
    incentive for the *second* and *third* item of a rare category rather
    than declaring the problem solved after one.
    """
    if total <= 0:
        return 1.0
    share = counts.get(key, 0) / total
    return max(0.0, 1.0 - math.sqrt(min(1.0, share * 2.0)))


def _quota_penalty(counts: Counter, key: str, total: int, cap_share: float) -> float:
    """
    Soft quota. Zero below the cap, rising toward 1 as a dimension
    dominates. Soft rather than hard so a genuinely valuable document is
    never permanently blocked by its language being popular.
    """
    if total < 10:
        return 0.0
    share = counts.get(key, 0) / total
    if share <= cap_share:
        return 0.0
    return min(1.0, (share - cap_share) / max(0.01, 1.0 - cap_share))


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    score: float
    components: dict[str, float]
    rationale: str


class Curator:
    """
    Scores and ranks admissible candidates.

    `seed` makes the whole thing reproducible: the same seed, corpus
    profile, and candidate pool always produce the same ranking, which is
    what makes a pilot run repeatable and a regression in selection
    visible.
    """

    def __init__(
        self,
        weights: SelectionWeights | None = None,
        *,
        seed: int = 0,
        exploration: float = 0.05,
        language_cap_share: float = 0.5,
        source_cap_share: float = 0.6,
        priority_languages: tuple[str, ...] = PRIORITY_LANGUAGES,
    ) -> None:
        self.weights = weights or SelectionWeights()
        self.seed = seed
        self.exploration = max(0.0, min(0.5, exploration))
        self.language_cap_share = language_cap_share
        self.source_cap_share = source_cap_share
        self.priority_languages = priority_languages

    # ------------------------------------------------------------- scoring

    def score(
        self,
        candidate: Candidate,
        profile: CorpusProfile,
        *,
        source_trust: float = 0.5,
        license_certainty: float = 1.0,
        duplication_risk: float = 0.0,
    ) -> ScoredCandidate:
        components: dict[str, float] = {}
        w = self.weights

        components["license_certainty"] = license_certainty
        components["source_confidence"] = source_trust

        components["expected_quality"] = self._expected_quality(candidate)
        components["metadata_completeness"] = self._metadata_completeness(candidate)

        language = (candidate.language or "unknown").lower()
        components["novelty"] = self._novelty(candidate, profile)
        components["language_diversity"] = self._language_score(language, profile)
        components["geographic_diversity"] = _diversity_bonus(
            profile.source_counts, candidate.source_id, profile.total
        )
        century = _century_of(candidate.publication_date)
        components["chronological_diversity"] = (
            _diversity_bonus(profile.century_counts, century, profile.total) if century else 0.4
        )
        components["author_diversity"] = self._author_score(candidate, profile)
        traditions = _traditions_from_subjects(list(candidate.subjects) + [candidate.title])
        components["tradition_diversity"] = (
            max((_diversity_bonus(profile.tradition_counts, t, profile.total) for t in traditions), default=0.3)
        )

        components["thematic_relevance"] = self._thematic_relevance(candidate, traditions)
        components["canonical_value"] = self._canonical_value(candidate)
        components["download_cost"] = self._download_cost(candidate)
        components["duplication_risk"] = duplication_risk
        components["ocr_risk"] = self._ocr_risk(candidate)

        base = sum(getattr(w, name, 0.0) * value for name, value in components.items())

        # Soft quotas subtract after the weighted sum, so they suppress
        # rather than distort the individual components.
        penalty = 0.0
        penalty += 0.25 * _quota_penalty(profile.language_counts, language, profile.total, self.language_cap_share)
        penalty += 0.20 * _quota_penalty(
            profile.source_counts, candidate.source_id, profile.total, self.source_cap_share
        )
        components["quota_penalty"] = -penalty

        jitter = self._jitter(candidate)
        components["exploration"] = jitter

        total = base - penalty + jitter
        return ScoredCandidate(
            candidate=candidate,
            score=round(total, 6),
            components={k: round(v, 4) for k, v in components.items()},
            rationale=self._rationale(components, traditions, language),
        )

    # ---------------------------------------------------------- components

    def _expected_quality(self, candidate: Candidate) -> float:
        """
        Format is the best available proxy before download. TXT and EPUB
        from a structured catalogue are near-certain to be clean; a PDF
        is a coin flip on its text layer.
        """
        from .normalization import rank_format

        rank = rank_format(candidate.download_format)
        by_rank = {0: 1.0, 1: 0.95, 2: 0.8, 3: 0.8, 4: 0.75, 5: 0.75, 6: 0.45}
        return by_rank.get(rank, 0.3)

    def _metadata_completeness(self, candidate: Candidate) -> float:
        present = sum(
            1
            for value in (
                candidate.title, candidate.authors, candidate.language,
                candidate.publication_date, candidate.subjects, candidate.canonical_url,
                candidate.document_type,
            )
            if value
        )
        return present / 7.0

    def _novelty(self, candidate: Candidate, profile: CorpusProfile) -> float:
        from .deduplication import work_key

        key = work_key(candidate.title, list(candidate.authors), candidate.language)
        if key in profile.work_keys:
            return 0.0
        author_hits = sum(profile.author_counts.get(_author_key(a), 0) for a in candidate.authors)
        if author_hits == 0:
            return 1.0
        return max(0.2, 1.0 / (1.0 + math.log1p(author_hits)))

    def _author_score(self, candidate: Candidate, profile: CorpusProfile) -> float:
        """
        How much this document broadens the corpus's range of authors.

        Scored over the candidate's *least* represented author rather
        than an average: a collaboration between a heavily-collected
        author and an unrepresented one still brings someone new, and
        averaging would hide that. An anonymous work scores mid-range --
        it is neither a new voice nor a repeat of a known one.
        """
        if not candidate.authors:
            return 0.5
        return max(
            _diversity_bonus(profile.author_counts, _author_key(author), profile.total)
            for author in candidate.authors
        )

    def _language_score(self, language: str, profile: CorpusProfile) -> float:
        bonus = _diversity_bonus(profile.language_counts, language, profile.total)
        if language in self.priority_languages:
            return min(1.0, bonus * 0.7 + 0.3)
        if language in ("", "unknown"):
            # Not a penalty for being unusual -- a penalty for being
            # unstated, which makes every downstream decision worse.
            return 0.2
        # A non-priority language still earns its diversity bonus; the
        # corpus is not meant to be six languages forever.
        return bonus * 0.8

    def _thematic_relevance(self, candidate: Candidate, traditions: list[str]) -> float:
        if not traditions:
            return 0.15
        blob = " ".join([candidate.title, *candidate.subjects, candidate.document_type]).lower()
        hits = sum(1 for term in _FLAT_THEMES if term in blob)
        return min(1.0, 0.4 + 0.15 * min(hits, 4))

    def _canonical_value(self, candidate: Candidate) -> float:
        """
        Historical or canonical weight, discounted for the handful of
        works every catalogue already carries several editions of.
        """
        title = (candidate.title or "").lower()
        if any(marker in title for marker in _OVERREPRESENTED_TITLES):
            return 0.15
        score = 0.5
        if candidate.publication_date and _century_of(candidate.publication_date):
            century = int(_century_of(candidate.publication_date))
            if century <= 18:
                score += 0.3
            elif century == 19:
                score += 0.15
        return min(1.0, score)

    def _download_cost(self, candidate: Candidate) -> float:
        """0 for a small file, approaching 1 for a very large one."""
        size = candidate.estimated_bytes or 0
        if size <= 0:
            return 0.2
        return min(1.0, size / (50 * 1024 * 1024))

    def _ocr_risk(self, candidate: Candidate) -> float:
        fmt = (candidate.download_format or "").lower()
        if fmt == "pdf":
            return 0.7
        if fmt in ("txt", "epub"):
            return 0.05
        if fmt in ("html", "xhtml", "xml", "tei"):
            return 0.15
        return 0.5

    def _jitter(self, candidate: Candidate) -> float:
        """
        Seeded per-candidate exploration.

        Derived from a hash of (seed, document key) rather than drawn from
        a stream, so the jitter for one candidate does not depend on how
        many candidates were scored before it -- which is what makes the
        ranking stable when the pool changes.
        """
        if self.exploration <= 0:
            return 0.0
        digest = hashlib.blake2b(
            f"{self.seed}|{candidate.document_key()}".encode(), digest_size=8
        ).digest()
        rng = random.Random(int.from_bytes(digest, "big"))
        return rng.uniform(-self.exploration, self.exploration)

    def _rationale(self, components: dict[str, float], traditions: list[str], language: str) -> str:
        top = sorted(
            ((name, value) for name, value in components.items() if name not in ("exploration",)),
            key=lambda kv: abs(kv[1] * getattr(self.weights, kv[0], 0.0)),
            reverse=True,
        )[:3]
        parts = [f"{name}={value:.2f}" for name, value in top]
        if traditions:
            parts.append(f"traditions={'/'.join(traditions[:3])}")
        parts.append(f"lang={language}")
        return "; ".join(parts)

    # ------------------------------------------------------------- ranking

    def rank(
        self,
        candidates: list[Candidate],
        profile: CorpusProfile,
        *,
        source_trust: dict[str, float] | None = None,
        license_certainty: dict[str, float] | None = None,
        limit: int | None = None,
    ) -> list[ScoredCandidate]:
        """
        Score every candidate and return them best-first.

        The corpus profile is updated *as the ranking is built*, so a run
        that selects fifty documents does not select fifty French ones
        because French was under-represented when scoring began. This
        greedy re-profiling is what makes the diversity terms actually
        diversify within a single run rather than only between runs.
        """
        source_trust = source_trust or {}
        license_certainty = license_certainty or {}

        remaining = list(candidates)
        working = CorpusProfile(
            language_counts=Counter(profile.language_counts),
            source_counts=Counter(profile.source_counts),
            century_counts=Counter(profile.century_counts),
            author_counts=Counter(profile.author_counts),
            tradition_counts=Counter(profile.tradition_counts),
            work_keys=set(profile.work_keys),
            total=profile.total,
        )

        selected: list[ScoredCandidate] = []
        target = limit if limit is not None else len(remaining)

        while remaining and len(selected) < target:
            scored = [
                self.score(
                    candidate,
                    working,
                    source_trust=source_trust.get(candidate.source_id, 0.5),
                    license_certainty=license_certainty.get(candidate.document_key(), 1.0),
                )
                for candidate in remaining
            ]
            # Ties broken by document key so the ordering is total and
            # reproducible, never dependent on list order.
            scored.sort(key=lambda s: (-s.score, s.candidate.document_key()))
            best = scored[0]
            selected.append(best)
            remaining = [c for c in remaining if c.document_key() != best.candidate.document_key()]
            _absorb(working, best.candidate)

        return selected


def _absorb(profile: CorpusProfile, candidate: Candidate) -> None:
    profile.total += 1
    profile.language_counts[(candidate.language or "unknown").lower()] += 1
    profile.source_counts[candidate.source_id] += 1
    century = _century_of(candidate.publication_date)
    if century:
        profile.century_counts[century] += 1
    for author in candidate.authors:
        profile.author_counts[_author_key(author)] += 1
    for tradition in _traditions_from_subjects(list(candidate.subjects) + [candidate.title]):
        profile.tradition_counts[tradition] += 1

    from .deduplication import work_key

    profile.work_keys.add(work_key(candidate.title, list(candidate.authors), candidate.language))


def coverage_report(profile: CorpusProfile) -> dict:
    """Coverage metrics for the run report and `corpus status`."""

    def _shares(counts: Counter) -> dict[str, float]:
        total = max(1, profile.total)
        return {k: round(v / total, 4) for k, v in counts.most_common(20)}

    return {
        "total_documents": profile.total,
        "languages": _shares(profile.language_counts),
        "sources": _shares(profile.source_counts),
        "centuries": _shares(profile.century_counts),
        "traditions": _shares(profile.tradition_counts),
        "distinct_authors": len(profile.author_counts),
        "distinct_works": len(profile.work_keys),
        "priority_language_coverage": {
            lang: profile.language_counts.get(lang, 0) for lang in PRIORITY_LANGUAGES
        },
    }


__all__ = [
    "PRIORITY_LANGUAGES",
    "THEMATIC_PROFILE",
    "CorpusProfile",
    "Curator",
    "ScoredCandidate",
    "SelectionWeights",
    "coverage_report",
]
