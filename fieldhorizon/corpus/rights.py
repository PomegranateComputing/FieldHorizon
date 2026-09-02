"""
The rights engine: deterministic, evidence-based, and never an LLM.

Two rules dominate this module and explain most of its shape.

**No evidence, no accept.** `evaluate()` cannot return `accept` for a
signal that carries no `Evidence`. Not "should not" -- cannot; the guard
runs after every other branch and downgrades to quarantine. A licence
string that nobody can point at a URL for is a rumour, not a licence.

**Age is not evidence.** A publication date of 1850 and an author who
died in 1890 are enriching context. They are never, on their own, grounds
for a public-domain verdict: the term of copyright depends on
jurisdiction, edition, translation, and editorial apparatus, none of
which a year can settle. `AGE_BASED_INFERENCE_ONLY` /
`AUTHOR_DEATH_INFERENCE_ONLY` exist to record that a decision was
*refused* on exactly this basis.

The third structural rule is the metadata/content split. Europeana
publishes its metadata under CC0; that says nothing whatsoever about the
digital object. `RightsSignal` keeps the two apart and this module only
ever adjudicates `content_license`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .models import (
    RIGHTS_POLICY_VERSION,
    Decision,
    DistributionScope,
    Evidence,
    ReasonCode,
    RightsDecision,
    RightsSignal,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Normalized licence vocabulary
# --------------------------------------------------------------------------

LIC_PUBLIC_DOMAIN = "public-domain"
LIC_PUBLIC_DOMAIN_US = "public-domain-us"
LIC_PDM = "cc-pdm-1.0"
LIC_CC0 = "cc0-1.0"
LIC_CC_BY = "cc-by"
LIC_CC_BY_SA = "cc-by-sa"
LIC_CC_BY_NC = "cc-by-nc"
LIC_CC_BY_ND = "cc-by-nd"
LIC_CC_BY_NC_SA = "cc-by-nc-sa"
LIC_CC_BY_NC_ND = "cc-by-nc-nd"
LIC_US_GOV = "us-federal-government-work"
LIC_GUTENBERG_US = "gutenberg-us-public-domain"
LIC_IN_COPYRIGHT = "in-copyright"
LIC_COPYRIGHT_NOT_EVALUATED = "copyright-not-evaluated"
LIC_NO_COPYRIGHT_NC = "no-copyright-non-commercial-only"
LIC_ALL_RIGHTS_RESERVED = "all-rights-reserved"
LIC_UNKNOWN = "unknown"


@dataclass(frozen=True)
class LicenseFacts:
    """What a normalized licence permits, independent of any policy."""

    normalized: str
    commercial_use: bool
    redistribution: bool
    derivatives: bool
    attribution_required: bool
    share_alike: bool
    #: Whether the licence itself establishes worldwide scope. Public
    #: domain "in the United States" does not; CC0 does.
    inherent_scope: DistributionScope
    #: The reason code an accept under this licence would carry.
    accept_reason: ReasonCode | None


_FACTS: dict[str, LicenseFacts] = {
    LIC_PUBLIC_DOMAIN: LicenseFacts(
        LIC_PUBLIC_DOMAIN, True, True, True, False, False, DistributionScope.WORLDWIDE,
        ReasonCode.EXPLICIT_PUBLIC_DOMAIN,
    ),
    LIC_PUBLIC_DOMAIN_US: LicenseFacts(
        LIC_PUBLIC_DOMAIN_US, True, True, True, False, False, DistributionScope.LOCAL_US_ONLY,
        ReasonCode.EXPLICIT_PUBLIC_DOMAIN,
    ),
    LIC_PDM: LicenseFacts(
        LIC_PDM, True, True, True, False, False, DistributionScope.WORLDWIDE, ReasonCode.PUBLIC_DOMAIN_MARK
    ),
    LIC_CC0: LicenseFacts(LIC_CC0, True, True, True, False, False, DistributionScope.WORLDWIDE, ReasonCode.CC0),
    LIC_CC_BY: LicenseFacts(LIC_CC_BY, True, True, True, True, False, DistributionScope.WORLDWIDE, ReasonCode.CC_BY),
    LIC_CC_BY_SA: LicenseFacts(
        LIC_CC_BY_SA, True, True, True, True, True, DistributionScope.WORLDWIDE, ReasonCode.CC_BY_SA
    ),
    LIC_US_GOV: LicenseFacts(
        LIC_US_GOV, True, True, True, False, False, DistributionScope.LOCAL_US_ONLY,
        ReasonCode.US_FEDERAL_GOVERNMENT_WORK,
    ),
    # Project Gutenberg's own terms are United-States-centric by their own
    # statement. Accepting one as worldwide public domain is precisely the
    # inference the brief forbids.
    LIC_GUTENBERG_US: LicenseFacts(
        LIC_GUTENBERG_US, True, True, True, False, False, DistributionScope.LOCAL_US_ONLY,
        ReasonCode.EXPLICIT_PUBLIC_DOMAIN,
    ),
    # Everything below is never acceptable under any shipped profile; the
    # facts are recorded so a rejection can say precisely what was wrong.
    LIC_CC_BY_NC: LicenseFacts(LIC_CC_BY_NC, False, True, True, True, False, DistributionScope.UNKNOWN, None),
    LIC_CC_BY_ND: LicenseFacts(LIC_CC_BY_ND, True, True, False, True, False, DistributionScope.UNKNOWN, None),
    LIC_CC_BY_NC_SA: LicenseFacts(LIC_CC_BY_NC_SA, False, True, True, True, True, DistributionScope.UNKNOWN, None),
    LIC_CC_BY_NC_ND: LicenseFacts(LIC_CC_BY_NC_ND, False, True, False, True, False, DistributionScope.UNKNOWN, None),
    LIC_IN_COPYRIGHT: LicenseFacts(LIC_IN_COPYRIGHT, False, False, False, False, False, DistributionScope.UNKNOWN, None),
    LIC_COPYRIGHT_NOT_EVALUATED: LicenseFacts(
        LIC_COPYRIGHT_NOT_EVALUATED, False, False, False, False, False, DistributionScope.UNKNOWN, None
    ),
    LIC_NO_COPYRIGHT_NC: LicenseFacts(
        LIC_NO_COPYRIGHT_NC, False, False, False, False, False, DistributionScope.UNKNOWN, None
    ),
    LIC_ALL_RIGHTS_RESERVED: LicenseFacts(
        LIC_ALL_RIGHTS_RESERVED, False, False, False, False, False, DistributionScope.UNKNOWN, None
    ),
    LIC_UNKNOWN: LicenseFacts(LIC_UNKNOWN, False, False, False, False, False, DistributionScope.UNKNOWN, None),
}


def facts_for(normalized: str) -> LicenseFacts:
    return _FACTS.get(normalized, _FACTS[LIC_UNKNOWN])


# --------------------------------------------------------------------------
# Licence normalization
# --------------------------------------------------------------------------

# rightsstatements.org URIs are exact identifiers, matched before any
# fuzzy text matching. These are the statements Europeana and its
# aggregators actually emit.
_RIGHTS_STATEMENT_URIS: dict[str, str] = {
    "http://rightsstatements.org/vocab/inc/1.0/": LIC_IN_COPYRIGHT,
    "http://rightsstatements.org/vocab/inc-ow-eu/1.0/": LIC_IN_COPYRIGHT,
    "http://rightsstatements.org/vocab/inc-edu/1.0/": LIC_IN_COPYRIGHT,
    "http://rightsstatements.org/vocab/inc-nc/1.0/": LIC_NO_COPYRIGHT_NC,
    "http://rightsstatements.org/vocab/noc-nc/1.0/": LIC_NO_COPYRIGHT_NC,
    "http://rightsstatements.org/vocab/noc-oklr/1.0/": LIC_COPYRIGHT_NOT_EVALUATED,
    "http://rightsstatements.org/vocab/cne/1.0/": LIC_COPYRIGHT_NOT_EVALUATED,
    "http://rightsstatements.org/vocab/undetermined/1.0/": LIC_UNKNOWN,
    "http://rightsstatements.org/vocab/unknown/1.0/": LIC_UNKNOWN,
    "http://rightsstatements.org/vocab/nkc/1.0/": LIC_PUBLIC_DOMAIN,
}

_CC_URI = re.compile(
    r"creativecommons\.org/(?:licenses|publicdomain)/(?P<code>[a-z0-9\-]+)(?:/(?P<version>[0-9.]+))?",
    re.IGNORECASE,
)

# Free-text phrases that carry no legal weight on their own. A provider
# saying "free ebook" is marketing; it is not a licence grant.
_VAGUE_PHRASES = (
    "free", "free to read", "free download", "open", "open access", "gratis",
    "libre", "gratuit", "no cost", "freely available",
)


def normalize_license(raw: str, rights_statement_uri: str = "") -> tuple[str, list[ReasonCode]]:
    """
    Map a provider's rights text or URI onto the normalized vocabulary.

    Returns (normalized_license, extra_reason_codes). The reason codes
    capture *why* something failed to normalize into something usable --
    a vague "free" claim is reported as VAGUE_FREE_CLAIM rather than
    silently becoming `unknown`, so the eventual rejection can explain
    itself.
    """
    codes: list[ReasonCode] = []
    text = (raw or "").strip()
    uri = (rights_statement_uri or "").strip()

    # 1. rightsstatements.org URI -- exact, authoritative, checked first.
    for candidate in (uri, text):
        normalized_uri = candidate.rstrip("/").lower() + "/"
        if normalized_uri in _RIGHTS_STATEMENT_URIS:
            mapped = _RIGHTS_STATEMENT_URIS[normalized_uri]
            if mapped == LIC_COPYRIGHT_NOT_EVALUATED:
                codes.append(ReasonCode.COPYRIGHT_NOT_EVALUATED)
            elif mapped == LIC_IN_COPYRIGHT:
                codes.append(ReasonCode.IN_COPYRIGHT)
            elif mapped == LIC_NO_COPYRIGHT_NC:
                codes.append(ReasonCode.NON_COMMERCIAL_RESTRICTION)
            elif mapped == LIC_UNKNOWN:
                codes.append(ReasonCode.UNKNOWN_LICENSE)
            return mapped, codes

    # 2. A creativecommons.org URI anywhere in either field.
    for candidate in (uri, text):
        match = _CC_URI.search(candidate or "")
        if match:
            code = match.group("code").lower()
            cc_mapped = _CC_CODE_MAP.get(code)
            if cc_mapped:
                codes.extend(_restriction_codes(cc_mapped))
                return cc_mapped, codes

    if not text:
        return LIC_UNKNOWN, [ReasonCode.NO_LICENSE_STATEMENT]

    low = " ".join(text.lower().split())

    # 3. Explicit phrases, longest/most specific first.
    for pattern, mapped in _TEXT_PATTERNS:
        if pattern.search(low):
            codes.extend(_restriction_codes(mapped))
            return mapped, codes

    # 4. A bare "free"/"open" claim with nothing else in it.
    if low in _VAGUE_PHRASES or all(word in _VAGUE_PHRASES for word in [low]):
        return LIC_UNKNOWN, [ReasonCode.VAGUE_FREE_CLAIM]
    if len(low) < 40 and any(low.startswith(p) for p in ("free", "open", "libre", "gratuit")):
        return LIC_UNKNOWN, [ReasonCode.VAGUE_FREE_CLAIM]

    return LIC_UNKNOWN, [ReasonCode.UNKNOWN_LICENSE]


_CC_CODE_MAP = {
    "zero": LIC_CC0,
    "cc0": LIC_CC0,
    "mark": LIC_PDM,
    "by": LIC_CC_BY,
    "by-sa": LIC_CC_BY_SA,
    "by-nc": LIC_CC_BY_NC,
    "by-nd": LIC_CC_BY_ND,
    "by-nc-sa": LIC_CC_BY_NC_SA,
    "by-nc-nd": LIC_CC_BY_NC_ND,
}


def _restriction_codes(normalized: str) -> list[ReasonCode]:
    codes: list[ReasonCode] = []
    if "nc" in normalized.split("-"):
        codes.append(ReasonCode.NON_COMMERCIAL_RESTRICTION)
    if "nd" in normalized.split("-"):
        codes.append(ReasonCode.NO_DERIVATIVES_RESTRICTION)
    return codes


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# Order matters: the first match wins, so narrower phrases precede broader
# ones ("public domain in the united states" before "public domain").
_TEXT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_p(r"\ball rights reserved\b|\btous droits r[ée]serv[ée]s\b"), LIC_ALL_RIGHTS_RESERVED),
    (_p(r"\bcopyright not evaluated\b|\bdroits non [ée]valu[ée]s\b"), LIC_COPYRIGHT_NOT_EVALUATED),
    (_p(r"\bin copyright\b|\bsous droit d'auteur\b|\bstill under copyright\b"), LIC_IN_COPYRIGHT),
    (_p(r"non[- ]commercial use only|usage non commercial"), LIC_NO_COPYRIGHT_NC),
    # An explicit jurisdictional limitation is checked BEFORE CC0, and the
    # order is load-bearing. Standard Ebooks writes, in one field:
    #
    #   "Public domain in the United States. Users located outside of the
    #    United States must check their local laws... Original content
    #    released to the public domain via the Creative Commons CC0 1.0
    #    Universal Public Domain Dedication."
    #
    # The CC0 covers the EDITION; the WORK is US-only. With CC0 matched
    # first, that statement normalized to worldwide CC0 -- turning an
    # explicit "check your local laws" into a worldwide grant, which is
    # the single most consequential misreading available here.
    (
        _p(r"public domain in the (united states|usa|u\.s\.)|public domain \(us\)|domaine public aux [ée]tats[- ]unis"),
        LIC_PUBLIC_DOMAIN_US,
    ),
    (_p(r"\bcc0\b|creative commons zero|public domain dedication"), LIC_CC0),
    (_p(r"public domain mark"), LIC_PDM),
    (_p(r"\bcc[ -]by[ -]nc[ -]nd\b"), LIC_CC_BY_NC_ND),
    (_p(r"\bcc[ -]by[ -]nc[ -]sa\b"), LIC_CC_BY_NC_SA),
    (_p(r"\bcc[ -]by[ -]nc\b"), LIC_CC_BY_NC),
    (_p(r"\bcc[ -]by[ -]nd\b"), LIC_CC_BY_ND),
    (_p(r"\bcc[ -]by[ -]sa\b|attribution[- ]sharealike"), LIC_CC_BY_SA),
    (_p(r"\bcc[ -]by\b|creative commons attribution(?![- ]non)"), LIC_CC_BY),
    (_p(r"work of the (united states|u\.s\.) (federal )?government|u\.s\. government work"), LIC_US_GOV),
    (
        _p(r"public domain|domaine public|gemeinfrei|dominio p[uú]blico|pubblico dominio"),
        LIC_PUBLIC_DOMAIN,
    ),
]


# --------------------------------------------------------------------------
# Policy profiles
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RightsProfile:
    """
    A named policy. `allowed_licenses` is a hard allowlist -- anything not
    named is refused, so adding a licence to the vocabulary can never
    accidentally widen what a profile accepts.
    """

    name: str
    allowed_licenses: frozenset[str]
    #: Scopes the profile tolerates. A profile that demands WORLDWIDE will
    #: quarantine a US-only public-domain text rather than accept it.
    allowed_scopes: frozenset[DistributionScope]
    require_commercial_use: bool = False
    require_derivatives: bool = False
    description: str = ""
    #: How the scope of an accepted document is labelled when the licence
    #: itself does not establish worldwide scope.
    default_scope: DistributionScope = DistributionScope.UNKNOWN


PROFILE_LOCAL_RESEARCH_US = RightsProfile(
    name="local_research_us",
    allowed_licenses=frozenset(
        {
            LIC_PUBLIC_DOMAIN,
            LIC_PUBLIC_DOMAIN_US,
            LIC_PDM,
            LIC_CC0,
            LIC_CC_BY,
            LIC_CC_BY_SA,
            LIC_US_GOV,
            LIC_GUTENBERG_US,
        }
    ),
    allowed_scopes=frozenset({DistributionScope.WORLDWIDE, DistributionScope.LOCAL_US_ONLY}),
    description=(
        "Local research use in a United States context. Accepts material whose "
        "public-domain status is established for the US only; such documents are "
        "marked LOCAL_US_ONLY and can never be packaged by export-safe "
        "--rights-profile release_worldwide."
    ),
    default_scope=DistributionScope.LOCAL_US_ONLY,
)

PROFILE_RELEASE_WORLDWIDE = RightsProfile(
    name="release_worldwide",
    allowed_licenses=frozenset({LIC_PUBLIC_DOMAIN, LIC_PDM, LIC_CC0, LIC_CC_BY, LIC_CC_BY_SA}),
    allowed_scopes=frozenset({DistributionScope.WORLDWIDE}),
    require_commercial_use=True,
    require_derivatives=True,
    description=(
        "Worldwide redistribution, including commercial reuse. Requires a licence "
        "that itself establishes worldwide scope -- US-only public domain is "
        "quarantined here, never accepted."
    ),
    default_scope=DistributionScope.WORLDWIDE,
)

PROFILE_STRICT_PUBLIC_DOMAIN = RightsProfile(
    name="strict_public_domain",
    allowed_licenses=frozenset({LIC_PUBLIC_DOMAIN, LIC_PDM, LIC_CC0}),
    allowed_scopes=frozenset({DistributionScope.WORLDWIDE}),
    description=(
        "Only material that is explicitly public domain, Public Domain Mark, or "
        "CC0. No attribution-bearing licence is accepted, however open."
    ),
    default_scope=DistributionScope.WORLDWIDE,
)

PROFILES: dict[str, RightsProfile] = {
    p.name: p for p in (PROFILE_LOCAL_RESEARCH_US, PROFILE_RELEASE_WORLDWIDE, PROFILE_STRICT_PUBLIC_DOMAIN)
}


def get_profile(name: str) -> RightsProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"Unknown rights profile {name!r}. Available: {', '.join(sorted(PROFILES))}"
        ) from None


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


@dataclass
class _Working:
    """Mutable accumulator while a decision is being assembled."""

    reason_codes: list[ReasonCode] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, code: ReasonCode) -> None:
        if code not in self.reason_codes:
            self.reason_codes.append(code)


def evaluate(
    signal: RightsSignal,
    profile: RightsProfile,
    *,
    source_trusted_for_content_rights: bool = True,
    checked_at: str = "",
) -> RightsDecision:
    """
    Adjudicate one document's rights under one profile.

    `source_trusted_for_content_rights` is the Internet Archive lever: IA
    is a trustworthy *discovery* surface but is not, in general, a
    guarantor of the rights of the objects it hosts. An untrusted source
    can still produce an accept, but only via an institutional collection
    allowlist -- an anonymous uploader's assertion alone is quarantined
    (UNTRUSTED_UPLOADER_ASSERTION).
    """
    work = _Working()

    # Access restrictions short-circuit everything: whatever the metadata
    # claims, a document we are not permitted to download is not a
    # document we may harvest.
    if signal.borrow_only:
        work.add(ReasonCode.BORROW_ONLY)
        return _refuse(Decision.REJECT, LIC_UNKNOWN, work, profile, signal, checked_at)
    if signal.access_restricted:
        work.add(ReasonCode.ACCESS_RESTRICTED)
        return _refuse(Decision.REJECT, LIC_UNKNOWN, work, profile, signal, checked_at)

    normalized, norm_codes = normalize_license(signal.content_license, signal.rights_statement_uri)
    for code in norm_codes:
        work.add(code)

    # The metadata/content trap. A provider that publishes open metadata
    # and says nothing about the object has told us nothing about the
    # object -- and this is the single most common way an open-looking
    # record turns out to license nothing at all.
    if normalized == LIC_UNKNOWN and signal.metadata_license:
        metadata_normalized, _ = normalize_license(signal.metadata_license)
        if metadata_normalized != LIC_UNKNOWN:
            work.add(ReasonCode.METADATA_LICENSE_ONLY)
            return _refuse(Decision.QUARANTINE, LIC_UNKNOWN, work, profile, signal, checked_at)

    # Contradiction: two positive but incompatible content statements.
    contradiction = _detect_contradiction(signal, normalized)
    if contradiction:
        work.add(ReasonCode.CONTRADICTORY_METADATA)
        work.notes.append(contradiction)
        return _refuse(Decision.QUARANTINE, normalized, work, profile, signal, checked_at)

    lic_facts = facts_for(normalized)

    if normalized == LIC_UNKNOWN:
        if ReasonCode.VAGUE_FREE_CLAIM not in work.reason_codes:
            work.add(ReasonCode.UNKNOWN_LICENSE)
        return _refuse(Decision.QUARANTINE, normalized, work, profile, signal, checked_at)

    # Definitively closed licences are rejected outright rather than
    # quarantined: there is nothing further to establish about them.
    if normalized in (LIC_IN_COPYRIGHT, LIC_ALL_RIGHTS_RESERVED, LIC_NO_COPYRIGHT_NC):
        return _refuse(Decision.REJECT, normalized, work, profile, signal, checked_at)
    if normalized == LIC_COPYRIGHT_NOT_EVALUATED:
        return _refuse(Decision.QUARANTINE, normalized, work, profile, signal, checked_at)
    if normalized in (LIC_CC_BY_NC, LIC_CC_BY_ND, LIC_CC_BY_NC_SA, LIC_CC_BY_NC_ND):
        return _refuse(Decision.REJECT, normalized, work, profile, signal, checked_at)

    if normalized not in profile.allowed_licenses:
        work.add(ReasonCode.NOT_PERMITTED_BY_PROFILE)
        return _refuse(Decision.QUARANTINE, normalized, work, profile, signal, checked_at)

    if profile.require_commercial_use and not lic_facts.commercial_use:
        work.add(ReasonCode.NOT_PERMITTED_BY_PROFILE)
        return _refuse(Decision.QUARANTINE, normalized, work, profile, signal, checked_at)
    if profile.require_derivatives and not lic_facts.derivatives:
        work.add(ReasonCode.NOT_PERMITTED_BY_PROFILE)
        return _refuse(Decision.QUARANTINE, normalized, work, profile, signal, checked_at)

    scope = _resolve_scope(signal, lic_facts, profile)
    if scope not in profile.allowed_scopes:
        work.add(ReasonCode.SCOPE_NOT_ESTABLISHED_WORLDWIDE)
        return _refuse(Decision.QUARANTINE, normalized, work, profile, signal, checked_at)

    if not source_trusted_for_content_rights:
        work.add(ReasonCode.UNTRUSTED_UPLOADER_ASSERTION)
        return _refuse(Decision.QUARANTINE, normalized, work, profile, signal, checked_at)
    if signal.uploader_asserted_only:
        work.add(ReasonCode.UNTRUSTED_UPLOADER_ASSERTION)
        return _refuse(Decision.QUARANTINE, normalized, work, profile, signal, checked_at)

    # The evidence gate. Everything above could pass and this still
    # refuses: an accept with no recorded proof is exactly what rule 8-10
    # forbids, and "the provider's field said so" only counts when the
    # adapter recorded WHERE it said so.
    if not signal.evidence:
        work.add(ReasonCode.EVIDENCE_MISSING)
        return _refuse(Decision.QUARANTINE, normalized, work, profile, signal, checked_at)

    if lic_facts.accept_reason:
        work.add(lic_facts.accept_reason)

    return RightsDecision(
        decision=Decision.ACCEPT,
        normalized_license=normalized,
        rights_scope=scope,
        commercial_use=lic_facts.commercial_use,
        redistribution=lic_facts.redistribution,
        derivatives=lic_facts.derivatives,
        attribution_required=lic_facts.attribution_required,
        share_alike=lic_facts.share_alike,
        jurisdictions=tuple(signal.jurisdictions),
        evidence=tuple(signal.evidence),
        reason_codes=tuple(work.reason_codes),
        policy_profile=profile.name,
        policy_version=RIGHTS_POLICY_VERSION,
        evidence_checked_at=checked_at,
        notes="; ".join(work.notes),
    )


def _resolve_scope(signal: RightsSignal, lic_facts: LicenseFacts, profile: RightsProfile) -> DistributionScope:
    """
    The licence's inherent scope wins. A provider claiming worldwide scope
    over a US-only public-domain determination does not upgrade it -- the
    limitation is in the determination, not in the provider's opinion of
    it.
    """
    if lic_facts.inherent_scope == DistributionScope.LOCAL_US_ONLY:
        return DistributionScope.LOCAL_US_ONLY
    if lic_facts.inherent_scope == DistributionScope.WORLDWIDE:
        return DistributionScope.WORLDWIDE

    declared = (signal.provider_declared_scope or "").strip().upper()
    if declared in {s.value for s in DistributionScope}:
        return DistributionScope(declared)
    return profile.default_scope


def _detect_contradiction(signal: RightsSignal, normalized: str) -> str:
    """
    Two positive content-rights statements that cannot both be true.

    Only content-level statements are compared. A CC0 metadata licence
    alongside an in-copyright object is not a contradiction -- it is the
    normal, correct state of an aggregator record, and treating it as a
    conflict would quarantine most of Europeana for no reason.
    """
    if not signal.rights_statement_uri or not signal.content_license:
        return ""
    from_uri, _ = normalize_license("", signal.rights_statement_uri)
    from_text, _ = normalize_license(signal.content_license)
    if from_uri == LIC_UNKNOWN or from_text == LIC_UNKNOWN:
        return ""
    if from_uri == from_text:
        return ""

    open_set = {LIC_PUBLIC_DOMAIN, LIC_PUBLIC_DOMAIN_US, LIC_PDM, LIC_CC0, LIC_CC_BY, LIC_CC_BY_SA, LIC_US_GOV}
    closed_set = {LIC_IN_COPYRIGHT, LIC_ALL_RIGHTS_RESERVED, LIC_NO_COPYRIGHT_NC, LIC_COPYRIGHT_NOT_EVALUATED}
    if (from_uri in open_set and from_text in closed_set) or (from_uri in closed_set and from_text in open_set):
        return f"rights statement resolves to {from_uri!r} but licence text resolves to {from_text!r}"
    return ""


def _refuse(
    decision: Decision,
    normalized: str,
    work: _Working,
    profile: RightsProfile,
    signal: RightsSignal,
    checked_at: str,
) -> RightsDecision:
    """
    Build a refusal. Note that refusals still carry whatever evidence was
    collected: knowing *what* the provider said is exactly what makes a
    quarantine reviewable later.
    """
    lic_facts = facts_for(normalized)
    return RightsDecision(
        decision=decision,
        normalized_license=normalized,
        rights_scope=DistributionScope.UNKNOWN,
        commercial_use=False,
        redistribution=False,
        derivatives=False,
        attribution_required=lic_facts.attribution_required,
        share_alike=lic_facts.share_alike,
        jurisdictions=tuple(signal.jurisdictions),
        evidence=tuple(signal.evidence),
        reason_codes=tuple(work.reason_codes),
        policy_profile=profile.name,
        policy_version=RIGHTS_POLICY_VERSION,
        evidence_checked_at=checked_at,
        notes="; ".join(work.notes),
    )


def note_age_inference(publication_year: int | None, author_death_year: int | None) -> list[ReasonCode]:
    """
    Record that age/death-date information was *seen* and deliberately not
    used as grounds for a verdict.

    This function exists to make the refusal explicit and auditable rather
    than implicit in the absence of code. Callers attach the returned
    codes to a decision's dossier; nothing in `evaluate()` ever reads
    them as a positive signal.
    """
    codes: list[ReasonCode] = []
    if publication_year is not None:
        codes.append(ReasonCode.AGE_BASED_INFERENCE_ONLY)
    if author_death_year is not None:
        codes.append(ReasonCode.AUTHOR_DEATH_INFERENCE_ONLY)
    return codes


def evidence_from_field(kind: str, value: str, url: str = "", retrieved_at: str = "") -> Evidence:
    """
    Build an Evidence record with its own content hash, so a later audit
    can detect that the provider changed the statement without having to
    diff free text.
    """
    import hashlib

    digest = hashlib.sha256((value or "").encode("utf-8")).hexdigest()
    return Evidence(kind=kind, value=value, url=url, content_hash=digest, retrieved_at=retrieved_at)


__all__ = [
    "PROFILES",
    "RightsProfile",
    "evaluate",
    "evidence_from_field",
    "facts_for",
    "get_profile",
    "normalize_license",
    "note_age_inference",
]
