"""
Value types for the Autonomous Open Corpus Harvester.

Nothing here touches the database, the network, or the filesystem: these
are the vocabulary the rest of the subsystem speaks. The pipeline state
machine lives here too, as an explicit transition table rather than as
scattered string comparisons -- an illegal transition is a programming
error the repository refuses to persist, not something to discover later
by reading `corpus_state_events`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum

# Bumped whenever a change alters what the pipeline *produces* from the
# same input (normalizer output, classifier decision, rights verdict).
# Recorded on every state transition so a run's outputs can always be
# attributed to the code that made them.
PIPELINE_VERSION = "1.0.0"
NORMALIZER_VERSION = "1.0.0"
CLASSIFIER_VERSION = "fh-corpus-classifier-1.0.0"
RIGHTS_POLICY_VERSION = "1.0.0"


class State(StrEnum):
    """
    The persistent pipeline state of one candidate/item.

    Terminal states (nothing transitions out of them) are RIGHTS_REJECTED,
    QUALITY_REJECTED, DUPLICATE, FAILED_FINAL, and WITHDRAWN. Everything
    else is resumable: a run that dies mid-download leaves an item in
    DOWNLOADING, and the next run picks it up from exactly there.
    """

    DISCOVERED = "DISCOVERED"
    RIGHTS_PENDING = "RIGHTS_PENDING"
    RIGHTS_ACCEPTED = "RIGHTS_ACCEPTED"
    RIGHTS_REJECTED = "RIGHTS_REJECTED"
    RIGHTS_QUARANTINED = "RIGHTS_QUARANTINED"
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    NORMALIZED = "NORMALIZED"
    QUALITY_REJECTED = "QUALITY_REJECTED"
    DUPLICATE = "DUPLICATE"
    CLASSIFIED_BOOKS = "CLASSIFIED_BOOKS"
    CLASSIFIED_MANIFESTO = "CLASSIFIED_MANIFESTO"
    MATERIALIZED = "MATERIALIZED"
    INGESTED = "INGESTED"
    INDEXED = "INDEXED"
    STALE = "STALE"
    WITHDRAWN = "WITHDRAWN"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"

    # ---- Phase II: acquisition-graph states -------------------------
    #
    # Phase I had no way to say "this provider described a document it
    # does not host", so DOAB's 103 rights-accepted records looked like
    # 103 download failures. They were not failures. These states let a
    # document rest in a legitimate, non-error condition that names what
    # is actually missing.

    #: Metadata (and possibly rights) are known; no provider hosts the
    #: text. A complete outcome for a discovery-only source.
    METADATA_ONLY = "METADATA_ONLY"
    #: A host exists but needs credentials that are not configured.
    AUTH_REQUIRED = "AUTH_REQUIRED"
    #: A host was named that is not allowlisted, or whose rights standing
    #: is not established.
    PROVIDER_UNVERIFIED = "PROVIDER_UNVERIFIED"
    #: Every candidate host was tried; none served the content.
    CONTENT_UNAVAILABLE = "CONTENT_UNAVAILABLE"
    #: The host answered and refused (403, lending, robots.txt).
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    #: Bytes are page images; text needs OCR that is unavailable or off.
    OCR_PENDING = "OCR_PENDING"
    #: The content lives in a bulk snapshot not yet fetched.
    BULK_SNAPSHOT_PENDING = "BULK_SNAPSHOT_PENDING"
    #: Every role is filled and acquisition may proceed.
    READY_FOR_DOWNLOAD = "READY_FOR_DOWNLOAD"


#: States from which an item can never move again.
TERMINAL_STATES: frozenset[State] = frozenset(
    {
        State.RIGHTS_REJECTED,
        State.QUALITY_REJECTED,
        State.DUPLICATE,
        State.WITHDRAWN,
        State.FAILED_FINAL,
    }
)

#: States whose documents are allowed to exist in the active retrieval
#: index. Deliberately narrow: a document is only ever indexable *after*
#: an accept rights decision, materialization, and ingestion. Quarantine,
#: staleness, and withdrawal all fall outside this set (rule 11).
INDEXABLE_STATES: frozenset[State] = frozenset({State.INGESTED, State.INDEXED})

#: Acquisition-graph states that are NOT errors. A source that describes
#: documents it does not host has done its job completely; reporting that
#: as a failure is what made DOAB look broken in Phase I. These states
#: are counted separately in run reports and never raise the error count.
NON_ERROR_ACQUISITION_STATES: frozenset[State] = frozenset(
    {
        State.METADATA_ONLY,
        State.AUTH_REQUIRED,
        State.BULK_SNAPSHOT_PENDING,
        State.OCR_PENDING,
        State.READY_FOR_DOWNLOAD,
    }
)

#: Acquisition-graph states that record a real obstacle. Distinguished
#: from the above so `corpus status` can separate "waiting on a
#: credential we could supply" from "the provider refused us".
BLOCKED_ACQUISITION_STATES: frozenset[State] = frozenset(
    {State.PROVIDER_UNVERIFIED, State.CONTENT_UNAVAILABLE, State.ACCESS_BLOCKED}
)

#: Legal transitions. Read as: from -> everything it may become.
#: FAILED_RETRYABLE is reachable from every non-terminal state (a network
#: or parse failure can happen at any step) and returns to the state it
#: came from, which is why it is written explicitly per-state rather than
#: as a wildcard -- the repository must be able to *reject* a nonsense
#: transition, and a wildcard would reject nothing.
_TRANSITIONS: dict[State, frozenset[State]] = {
    State.DISCOVERED: frozenset({State.RIGHTS_PENDING, State.FAILED_RETRYABLE, State.FAILED_FINAL}),
    State.RIGHTS_PENDING: frozenset(
        {
            State.RIGHTS_ACCEPTED,
            State.RIGHTS_REJECTED,
            State.RIGHTS_QUARANTINED,
            State.FAILED_RETRYABLE,
            State.FAILED_FINAL,
        }
    ),
    # An accepted item waits in RIGHTS_ACCEPTED until the curator selects
    # it (QUEUED). A later rights audit can still quarantine or withdraw it.
    #
    # Phase II adds the acquisition-graph outcomes: once rights are
    # settled, planning decides whether anyone can actually serve the
    # bytes, and the answer may legitimately be "nobody".
    # Planning happens here: "may we use this?" is settled, and "can
    # anyone actually serve it?" is asked next. Every acquisition-plan
    # outcome is reachable, because the plan can determine any of them
    # before a single byte is requested.
    State.RIGHTS_ACCEPTED: frozenset(
        {
            State.QUEUED,
            State.RIGHTS_QUARANTINED,
            State.STALE,
            State.WITHDRAWN,
            State.FAILED_FINAL,
            State.METADATA_ONLY,
            State.AUTH_REQUIRED,
            State.PROVIDER_UNVERIFIED,
            State.CONTENT_UNAVAILABLE,
            State.ACCESS_BLOCKED,
            State.OCR_PENDING,
            State.BULK_SNAPSHOT_PENDING,
            State.READY_FOR_DOWNLOAD,
        }
    ),
    # Quarantine is not a dead end: re-evaluation under a different policy
    # profile, or with new evidence, can release or definitively reject it.
    State.RIGHTS_QUARANTINED: frozenset(
        {State.RIGHTS_ACCEPTED, State.RIGHTS_REJECTED, State.WITHDRAWN, State.FAILED_FINAL}
    ),
    State.QUEUED: frozenset(
        {
            State.DOWNLOADING,
            State.RIGHTS_QUARANTINED,
            State.FAILED_RETRYABLE,
            State.FAILED_FINAL,
            State.CONTENT_UNAVAILABLE,
            State.ACCESS_BLOCKED,
        }
    ),
    State.DOWNLOADING: frozenset(
        {
            State.DOWNLOADED,
            State.QUEUED,
            State.FAILED_RETRYABLE,
            State.FAILED_FINAL,
            State.CONTENT_UNAVAILABLE,
            State.ACCESS_BLOCKED,
        }
    ),
    State.DOWNLOADED: frozenset(
        {
            State.NORMALIZED,
            State.DUPLICATE,
            State.QUALITY_REJECTED,
            State.FAILED_RETRYABLE,
            State.FAILED_FINAL,
            State.OCR_PENDING,
        }
    ),
    State.NORMALIZED: frozenset(
        {
            State.CLASSIFIED_BOOKS,
            State.CLASSIFIED_MANIFESTO,
            State.QUALITY_REJECTED,
            State.DUPLICATE,
            State.FAILED_RETRYABLE,
            State.FAILED_FINAL,
        }
    ),
    State.CLASSIFIED_BOOKS: frozenset(
        {State.MATERIALIZED, State.CLASSIFIED_MANIFESTO, State.DUPLICATE, State.FAILED_RETRYABLE, State.FAILED_FINAL}
    ),
    State.CLASSIFIED_MANIFESTO: frozenset(
        {State.MATERIALIZED, State.CLASSIFIED_BOOKS, State.DUPLICATE, State.FAILED_RETRYABLE, State.FAILED_FINAL}
    ),
    State.MATERIALIZED: frozenset({State.INGESTED, State.STALE, State.WITHDRAWN, State.FAILED_RETRYABLE, State.FAILED_FINAL}),
    State.INGESTED: frozenset({State.INDEXED, State.STALE, State.WITHDRAWN, State.MATERIALIZED, State.FAILED_RETRYABLE}),
    State.INDEXED: frozenset({State.STALE, State.WITHDRAWN, State.INGESTED, State.MATERIALIZED}),
    # STALE means "the rights evidence behind this document no longer
    # verifies". It leaves the active index immediately; re-verification
    # can restore it or a withdrawal can finish it.
    State.STALE: frozenset({State.RIGHTS_PENDING, State.RIGHTS_ACCEPTED, State.WITHDRAWN, State.RIGHTS_QUARANTINED}),
    State.FAILED_RETRYABLE: frozenset(
        {
            State.DISCOVERED,
            State.RIGHTS_PENDING,
            State.RIGHTS_ACCEPTED,
            State.QUEUED,
            State.DOWNLOADING,
            State.DOWNLOADED,
            State.NORMALIZED,
            State.CLASSIFIED_BOOKS,
            State.CLASSIFIED_MANIFESTO,
            State.MATERIALIZED,
            State.INGESTED,
            State.FAILED_FINAL,
        }
    ),
    State.RIGHTS_REJECTED: frozenset(),
    State.QUALITY_REJECTED: frozenset(),
    State.DUPLICATE: frozenset(),
    State.WITHDRAWN: frozenset(),
    State.FAILED_FINAL: frozenset(),

    # ---- Phase II: acquisition-graph states -------------------------
    #
    # None of these is terminal. Every one describes a condition that a
    # later run can resolve -- a credential appears, a dump is fetched,
    # an OCR worker is installed, a provider stops refusing. Making them
    # terminal would recreate exactly the Phase I problem of a source
    # being permanently "broken" for a reason that was never permanent.

    #: A discovery-only source's complete outcome. A later run may find a
    #: host for it (a second provider, a bulk snapshot).
    State.METADATA_ONLY: frozenset(
        {
            State.READY_FOR_DOWNLOAD,
            State.BULK_SNAPSHOT_PENDING,
            State.AUTH_REQUIRED,
            State.RIGHTS_QUARANTINED,
            State.WITHDRAWN,
        }
    ),
    State.AUTH_REQUIRED: frozenset(
        {State.READY_FOR_DOWNLOAD, State.METADATA_ONLY, State.PROVIDER_UNVERIFIED, State.WITHDRAWN}
    ),
    State.PROVIDER_UNVERIFIED: frozenset(
        {State.READY_FOR_DOWNLOAD, State.METADATA_ONLY, State.RIGHTS_QUARANTINED, State.WITHDRAWN}
    ),
    State.CONTENT_UNAVAILABLE: frozenset(
        {State.QUEUED, State.READY_FOR_DOWNLOAD, State.METADATA_ONLY, State.FAILED_FINAL, State.WITHDRAWN}
    ),
    #: The provider refused us. Retryable only after a delay, and never
    #: by working around the refusal.
    State.ACCESS_BLOCKED: frozenset(
        {State.QUEUED, State.METADATA_ONLY, State.FAILED_FINAL, State.WITHDRAWN}
    ),
    State.OCR_PENDING: frozenset(
        {State.NORMALIZED, State.QUALITY_REJECTED, State.FAILED_FINAL, State.WITHDRAWN}
    ),
    State.BULK_SNAPSHOT_PENDING: frozenset(
        {State.DOWNLOADED, State.READY_FOR_DOWNLOAD, State.METADATA_ONLY, State.FAILED_RETRYABLE, State.WITHDRAWN}
    ),
    State.READY_FOR_DOWNLOAD: frozenset(
        {
            State.QUEUED,
            State.DOWNLOADING,
            State.METADATA_ONLY,
            State.ACCESS_BLOCKED,
            State.CONTENT_UNAVAILABLE,
            State.RIGHTS_QUARANTINED,
            State.FAILED_RETRYABLE,
            State.FAILED_FINAL,
        }
    ),
}


class IllegalTransition(ValueError):
    """A state transition the pipeline's state machine does not define."""


def can_transition(old: State, new: State) -> bool:
    return new in _TRANSITIONS.get(old, frozenset())


def assert_transition(old: State, new: State) -> None:
    if not can_transition(old, new):
        raise IllegalTransition(f"{old.value} -> {new.value} is not a legal pipeline transition")


def allowed_transitions(old: State) -> frozenset[State]:
    return _TRANSITIONS.get(old, frozenset())


# --------------------------------------------------------------------------
# Rights vocabulary
# --------------------------------------------------------------------------


class Decision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    QUARANTINE = "quarantine"


class DistributionScope(StrEnum):
    """
    How far a document may travel. LOCAL_US_ONLY is the important one: a
    Gutenberg text whose only evidence is "public domain in the United
    States" is perfectly usable for local research and must never be
    packaged into a worldwide release (rule: export guard).
    """

    WORLDWIDE = "WORLDWIDE"
    LOCAL_US_ONLY = "LOCAL_US_ONLY"
    UNKNOWN = "UNKNOWN"


class ReasonCode(StrEnum):
    """
    Structured, machine-checkable reasons. Every rights decision carries a
    list of these; a free-text rationale is never the only record of why
    something was accepted or refused.
    """

    # Accept reasons
    EXPLICIT_PUBLIC_DOMAIN = "EXPLICIT_PUBLIC_DOMAIN"
    PUBLIC_DOMAIN_MARK = "PUBLIC_DOMAIN_MARK"
    CC0 = "CC0"
    CC_BY = "CC_BY"
    CC_BY_SA = "CC_BY_SA"
    US_FEDERAL_GOVERNMENT_WORK = "US_FEDERAL_GOVERNMENT_WORK"
    EXPLICIT_OPEN_LICENSE = "EXPLICIT_OPEN_LICENSE"
    PROVIDER_DECLARED_CONTENT_LICENSE = "PROVIDER_DECLARED_CONTENT_LICENSE"

    # Reject / quarantine reasons
    UNKNOWN_LICENSE = "UNKNOWN_LICENSE"
    NO_LICENSE_STATEMENT = "NO_LICENSE_STATEMENT"
    COPYRIGHT_NOT_EVALUATED = "COPYRIGHT_NOT_EVALUATED"
    IN_COPYRIGHT = "IN_COPYRIGHT"
    ALL_RIGHTS_RESERVED = "ALL_RIGHTS_RESERVED"
    NON_COMMERCIAL_RESTRICTION = "NON_COMMERCIAL_RESTRICTION"
    NO_DERIVATIVES_RESTRICTION = "NO_DERIVATIVES_RESTRICTION"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"
    BORROW_ONLY = "BORROW_ONLY"
    CONTRADICTORY_METADATA = "CONTRADICTORY_METADATA"
    VAGUE_FREE_CLAIM = "VAGUE_FREE_CLAIM"
    AGE_BASED_INFERENCE_ONLY = "AGE_BASED_INFERENCE_ONLY"
    AUTHOR_DEATH_INFERENCE_ONLY = "AUTHOR_DEATH_INFERENCE_ONLY"
    METADATA_LICENSE_ONLY = "METADATA_LICENSE_ONLY"
    PROVIDER_DISCLAIMS_WARRANTY = "PROVIDER_DISCLAIMS_WARRANTY"
    UNTRUSTED_UPLOADER_ASSERTION = "UNTRUSTED_UPLOADER_ASSERTION"
    SCOPE_NOT_ESTABLISHED_WORLDWIDE = "SCOPE_NOT_ESTABLISHED_WORLDWIDE"
    NOT_PERMITTED_BY_PROFILE = "NOT_PERMITTED_BY_PROFILE"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    SOURCE_NOT_ALLOWLISTED = "SOURCE_NOT_ALLOWLISTED"


@dataclass(frozen=True)
class Evidence:
    """
    One piece of recorded proof behind a rights decision.

    `kind` says what sort of proof it is (a rights statement URI, a
    licence header found in the file itself, a provider metadata field).
    `value` is the raw text as the provider wrote it -- never a
    paraphrase. `url` is where it can be re-checked. `content_hash` lets a
    later audit detect that the evidence *changed* without having to
    re-parse it.

    No evidence, no accept. That is enforced in rights.py, not here.
    """

    kind: str
    value: str
    url: str = ""
    content_hash: str = ""
    retrieved_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> Evidence:
        """
        Rebuild evidence from its serialized form.

        The counterpart to `to_dict`, and not optional: evidence that
        cannot be read back is evidence that vanishes the moment a rights
        stack round-trips through the database, and the evidence gate
        then quarantines a perfectly well-documented document while
        reporting nothing that would explain why.
        """
        return cls(
            kind=str(payload.get("kind", "")),
            value=str(payload.get("value", "")),
            url=str(payload.get("url", "")),
            content_hash=str(payload.get("content_hash", "")),
            retrieved_at=str(payload.get("retrieved_at", "")),
        )


@dataclass(frozen=True)
class RightsDecision:
    """
    The structured verdict the brief specifies, plus the fields needed to
    audit it later. `policy_version` and `evidence_checked_at` are what
    make `corpus audit-rights --stale-after-days` possible.
    """

    decision: Decision
    normalized_license: str
    rights_scope: DistributionScope
    commercial_use: bool
    redistribution: bool
    derivatives: bool
    attribution_required: bool
    share_alike: bool
    jurisdictions: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    reason_codes: tuple[ReasonCode, ...] = ()
    policy_profile: str = ""
    policy_version: str = RIGHTS_POLICY_VERSION
    evidence_checked_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "normalized_license": self.normalized_license,
            "rights_scope": self.rights_scope.value,
            "commercial_use": self.commercial_use,
            "redistribution": self.redistribution,
            "derivatives": self.derivatives,
            "attribution_required": self.attribution_required,
            "share_alike": self.share_alike,
            "jurisdictions": list(self.jurisdictions),
            "evidence": [e.to_dict() for e in self.evidence],
            "reason_codes": [c.value for c in self.reason_codes],
            "policy_profile": self.policy_profile,
            "policy_version": self.policy_version,
            "evidence_checked_at": self.evidence_checked_at,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# Discovery / candidate vocabulary
# --------------------------------------------------------------------------


class Destination(StrEnum):
    """
    The two Field Horizon corpora. These map to `data/books/_auto` and
    `data/manifestos/_auto` and to the `source_type` written into
    `sources` -- note the repo's plural directory and singular
    source_type, both preserved exactly.
    """

    BOOKS = "books"
    MANIFESTO = "manifesto"


@dataclass(frozen=True)
class RightsSignal:
    """
    What an adapter observed about rights, *before* any policy is applied.

    An adapter's job is to report faithfully, never to adjudicate: it
    fills in what the provider actually said and leaves the verdict to
    rights.py. `metadata_license` vs `content_license` is the distinction
    the brief insists on repeatedly -- Europeana's CC0 metadata licence
    says nothing about the digital object, and conflating them is exactly
    the failure mode this field exists to prevent.
    """

    content_license: str = ""
    metadata_license: str = ""
    rights_statement_uri: str = ""
    provider_declared_scope: str = ""
    jurisdictions: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    #: Set when the provider says the item cannot be freely downloaded
    #: (lending, access restrictions, "print disabled").
    access_restricted: bool = False
    borrow_only: bool = False
    #: True when the only positive signal is an uploader's free-text
    #: assertion rather than a structured field from the institution.
    uploader_asserted_only: bool = False
    #: Free-text the provider used for rights that did not normalize.
    raw_rights_text: str = ""


@dataclass(frozen=True)
class Candidate:
    """
    One discovered, not-yet-acquired document.

    Adapters emit these from catalogue records only -- discovery never
    downloads content. `download_url` is where the content *would* come
    from if the rights engine and curator both say yes.
    """

    source_id: str
    external_id: str
    title: str
    authors: tuple[str, ...] = ()
    contributors: tuple[str, ...] = ()
    translator: str = ""
    language: str = ""
    publication_date: str = ""
    edition_date: str = ""
    document_type: str = ""
    subjects: tuple[str, ...] = ()
    canonical_url: str = ""
    download_url: str = ""
    download_format: str = ""
    estimated_bytes: int = 0
    work_identifiers: tuple[str, ...] = ()
    rights: RightsSignal = field(default_factory=RightsSignal)
    raw_metadata: dict = field(default_factory=dict)
    #: Alternative download URLs in descending preference order, used when
    #: the primary format turns out to be unusable.
    alternate_downloads: tuple[tuple[str, str], ...] = ()

    def document_key(self) -> str:
        """Stable natural key for this candidate within its source."""
        return f"{self.source_id}:{self.external_id}"


@dataclass(frozen=True)
class QualityReport:
    score: float
    language_detected: str
    language_confidence: float
    char_count: int
    word_count: int
    issues: tuple[str, ...] = ()
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "language_detected": self.language_detected,
            "language_confidence": self.language_confidence,
            "char_count": self.char_count,
            "word_count": self.word_count,
            "issues": list(self.issues),
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True)
class Classification:
    destination: Destination
    manifesto_score: float
    confidence: float
    document_form: str = ""
    primary_domain: str = ""
    secondary_tags: tuple[str, ...] = ()
    normative_intent: float = 0.0
    mobilization_intent: float = 0.0
    doctrinal_intent: float = 0.0
    narrative_intent: float = 0.0
    analytical_intent: float = 0.0
    rationale: str = ""
    evidence: tuple[dict, ...] = ()
    classifier_version: str = CLASSIFIER_VERSION
    classifier_kind: str = "deterministic"
    low_confidence: bool = False

    def to_dict(self) -> dict:
        return {
            "destination": self.destination.value,
            "manifesto_score": self.manifesto_score,
            "confidence": self.confidence,
            "document_form": self.document_form,
            "primary_domain": self.primary_domain,
            "secondary_tags": list(self.secondary_tags),
            "normative_intent": self.normative_intent,
            "mobilization_intent": self.mobilization_intent,
            "doctrinal_intent": self.doctrinal_intent,
            "narrative_intent": self.narrative_intent,
            "analytical_intent": self.analytical_intent,
            "rationale": self.rationale,
            "evidence": [dict(e) for e in self.evidence],
            "classifier_version": self.classifier_version,
            "classifier_kind": self.classifier_kind,
            "low_confidence": self.low_confidence,
        }


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_length: int = 60) -> str:
    """
    Filesystem-neutral slug. Deliberately aggressive: harvested titles are
    untrusted input, and this result becomes a filename. Everything
    outside [a-z0-9-] is collapsed, so no path separator, no traversal
    sequence, no control character, and no shell metacharacter can survive
    (security rule: "noms de fichiers neutralisés").
    """
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_text).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "untitled"


#: How long to leave an ACCESS_BLOCKED item alone before asking again.
#: Six hours: long enough that a provider throttling us sees the pressure
#: stop, short enough that a transient block clears within a day. A 403
#: is the provider saying no, and the only correct answers are to wait
#: and to ask again politely -- never to ask differently.
ACCESS_BLOCKED_RETRY_AFTER_SECONDS = 6 * 60 * 60


