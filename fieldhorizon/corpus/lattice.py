"""
The rights lattice.

Phase I adjudicated one licence per document, taking the first pattern
that matched. The two most consequential defects the live pilot found
were both instances of that: Standard Ebooks states *"Public domain in
the United States"* for the **work** while dedicating its **edition**
under CC0, and the CC0 pattern matched first — turning an explicit
"check your local laws" into a worldwide grant.

Phase I fixed it by reordering regexes so the jurisdictional statement is
matched first. That works, and it is tested, but it is a fix at the wrong
level: it makes one pair of layers resolve correctly by ordering rather
than modelling the layers. The ordering became load-bearing, and the
documentation had to carry a standing warning saying so.

This module removes the need for that warning.

A document is a stack of `RightsComponent`s, each with its own licence.
Effective rights are the **intersection**: a permission is granted only
if every applicable component grants it, and the scope is the narrowest
any component imposes. A broader permission on a secondary component
cannot widen the primary content — not by convention, but because
intersection has no way to widen anything.

    source_text            PUBLIC_DOMAIN_US   scope LOCAL_US_ONLY
    editorial_contribution CC0                scope WORLDWIDE
    ─────────────────────────────────────────────────────────────
    effective                                 scope LOCAL_US_ONLY
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from .models import Decision, DistributionScope, Evidence, ReasonCode
from .rights import LIC_UNKNOWN, facts_for, normalize_license

logger = logging.getLogger(__name__)


class RightsComponent(StrEnum):
    """
    One layer of a document's rights.

    Ordered roughly from the work outward to its packaging. Which
    components are *applicable* depends on what the document actually is:
    a born-digital text has no scan and no OCR; a scanned translation has
    all of work, translation, scan, and OCR.
    """

    #: The underlying intellectual creation.
    WORK = "work"
    #: The specific text being reproduced (an edition of the work).
    SOURCE_TEXT = "source_text"
    #: A translation, which carries its own copyright distinct from the
    #: work's. This is why the deduplicator never merges translations.
    TRANSLATION = "translation"
    ILLUSTRATIONS = "illustrations"
    #: A transcriber's or typesetter's contribution -- Standard Ebooks'
    #: CC0 covers exactly this and nothing else.
    EDITORIAL_CONTRIBUTION = "editorial_contribution"
    #: The packaged digital object (an EPUB, a IIIF manifest).
    DIGITAL_EDITION = "digital_edition"
    #: The catalogue record. NEVER contributes permissions to content --
    #: see `CONTENT_COMPONENTS`.
    METADATA = "metadata"
    #: The page images.
    SCAN = "scan"
    #: Text produced from the scan by OCR.
    OCR = "ocr"


#: The components whose licences actually govern the TEXT we ingest.
#:
#: `METADATA` is deliberately excluded. Europeana publishes its metadata
#: under CC0; that says nothing whatsoever about the object, and letting
#: it into the intersection would let an aggregator's open catalogue
#: licence grant rights over content it does not own. `ILLUSTRATIONS` is
#: excluded for the opposite reason: we ingest text, so an illustration's
#: separate copyright does not restrict the text we actually take.
CONTENT_COMPONENTS = frozenset(
    {
        RightsComponent.WORK,
        RightsComponent.SOURCE_TEXT,
        RightsComponent.TRANSLATION,
        RightsComponent.EDITORIAL_CONTRIBUTION,
        RightsComponent.DIGITAL_EDITION,
        RightsComponent.SCAN,
        RightsComponent.OCR,
    }
)

#: Components that alone can never establish rights over the text. A
#: free scan of an in-copyright book does not make the book free; a
#: freely-licensed OCR layer over a protected work does not free the
#: work.
INSUFFICIENT_ALONE = frozenset({RightsComponent.SCAN, RightsComponent.OCR, RightsComponent.METADATA})

#: Scope narrowness, lowest is narrowest. Intersection takes the max.
_SCOPE_RANK = {
    DistributionScope.WORLDWIDE: 2,
    DistributionScope.LOCAL_US_ONLY: 1,
    DistributionScope.UNKNOWN: 0,
}
_RANK_TO_SCOPE = {rank: scope for scope, rank in _SCOPE_RANK.items()}


@dataclass(frozen=True)
class ComponentRights:
    """One component's licence, normalized, with its evidence."""

    component: RightsComponent
    raw_license: str = ""
    rights_statement_uri: str = ""
    normalized_license: str = LIC_UNKNOWN
    evidence: tuple[Evidence, ...] = ()
    #: Set when the provider explicitly says this component does not
    #: apply -- a born-digital text has no scan. Distinct from "unknown".
    not_applicable: bool = False
    notes: str = ""

    @classmethod
    def parse(
        cls,
        component: RightsComponent,
        raw_license: str = "",
        *,
        rights_statement_uri: str = "",
        evidence: tuple[Evidence, ...] = (),
        notes: str = "",
    ) -> ComponentRights:
        normalized, _ = normalize_license(raw_license, rights_statement_uri)
        return cls(
            component=component,
            raw_license=raw_license,
            rights_statement_uri=rights_statement_uri,
            normalized_license=normalized,
            evidence=evidence,
            notes=notes,
        )

    @classmethod
    def absent(cls, component: RightsComponent, note: str = "") -> ComponentRights:
        return cls(component=component, not_applicable=True, notes=note)

    def scope(self) -> DistributionScope:
        return facts_for(self.normalized_license).inherent_scope

    def to_dict(self) -> dict:
        return {
            "component": self.component.value,
            "raw_license": self.raw_license,
            "rights_statement_uri": self.rights_statement_uri,
            "normalized_license": self.normalized_license,
            "not_applicable": self.not_applicable,
            "evidence": [e.to_dict() for e in self.evidence],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> ComponentRights:
        """
        Rebuild a component from `to_dict`, **evidence included**.

        Found by the Standard Ebooks pilot: a stack rebuilt without its
        evidence still intersects to the right licence and the right
        scope, so every visible field looks correct -- and then the
        evidence gate quarantines all seventeen editions, because "no
        evidence, no accept" is absolute. The failure is silent, total,
        and indistinguishable from a genuine rights problem. Anything
        that persists a stack and reads it back must come through here.

        The normalized licence is taken from the payload rather than
        re-derived: re-normalizing on read would let a change in the
        licence vocabulary silently rewrite a stored verdict, which is
        exactly what `corpus audit-rights` exists to make visible.
        """
        return cls(
            component=RightsComponent(payload["component"]),
            raw_license=str(payload.get("raw_license", "")),
            rights_statement_uri=str(payload.get("rights_statement_uri", "")),
            normalized_license=str(payload.get("normalized_license", LIC_UNKNOWN)),
            evidence=tuple(Evidence.from_dict(e) for e in payload.get("evidence", ())),
            not_applicable=bool(payload.get("not_applicable", False)),
            notes=str(payload.get("notes", "")),
        )


@dataclass(frozen=True)
class LatticeResult:
    """
    Effective rights after intersecting every applicable component.

    `limiting_components` is the part an operator actually needs: it
    names which layer produced the narrowest answer, so "why is this
    LOCAL_US_ONLY" has a one-word reply.
    """

    commercial_use: bool
    redistribution: bool
    derivatives: bool
    attribution_required: bool
    share_alike: bool
    scope: DistributionScope
    effective_license: str
    components: tuple[ComponentRights, ...] = ()
    limiting_components: tuple[RightsComponent, ...] = ()
    reason_codes: tuple[ReasonCode, ...] = ()
    blocked: bool = False
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "commercial_use": self.commercial_use,
            "redistribution": self.redistribution,
            "derivatives": self.derivatives,
            "attribution_required": self.attribution_required,
            "share_alike": self.share_alike,
            "scope": self.scope.value,
            "effective_license": self.effective_license,
            "components": [c.to_dict() for c in self.components],
            "limiting_components": [c.value for c in self.limiting_components],
            "reason_codes": [r.value for r in self.reason_codes],
            "blocked": self.blocked,
            "notes": self.notes,
        }

    def all_evidence(self) -> tuple[Evidence, ...]:
        out: list[Evidence] = []
        for component in self.components:
            out.extend(component.evidence)
        return tuple(out)


@dataclass
class RightsStack:
    """
    A document's components, assembled by an adapter.

    Adapters state layers rather than picking a winner. That is the whole
    change: Standard Ebooks says "the work is US public domain" and "our
    edition is CC0" as two facts, and the lattice — not a regex ordering
    — decides what follows.
    """

    components: dict[RightsComponent, ComponentRights] = field(default_factory=dict)

    def set(self, component_rights: ComponentRights) -> RightsStack:
        self.components[component_rights.component] = component_rights
        return self

    def add(
        self,
        component: RightsComponent,
        raw_license: str = "",
        *,
        rights_statement_uri: str = "",
        evidence: tuple[Evidence, ...] = (),
        notes: str = "",
    ) -> RightsStack:
        return self.set(
            ComponentRights.parse(
                component, raw_license,
                rights_statement_uri=rights_statement_uri, evidence=evidence, notes=notes,
            )
        )

    def get(self, component: RightsComponent) -> ComponentRights | None:
        return self.components.get(component)

    def applicable(self) -> list[ComponentRights]:
        """
        Components that govern the ingested text and were actually
        stated. A component marked `not_applicable` is skipped; one that
        was never mentioned is simply absent.
        """
        return [
            c
            for component, c in self.components.items()
            if component in CONTENT_COMPONENTS and not c.not_applicable
        ]

    def to_dict(self) -> dict:
        return {c.value: r.to_dict() for c, r in sorted(self.components.items())}

    @classmethod
    def from_dict(cls, payload: dict) -> RightsStack:
        """
        Rebuild a stack from `to_dict`. A component naming a layer this
        version does not know is skipped rather than fatal -- a stack
        written by a newer build must not make an older one unable to
        read its own database -- but it is logged, because a silently
        dropped rights layer is precisely the kind of thing that widens
        a scope without anyone noticing.
        """
        stack = cls()
        for key, entry in (payload or {}).items():
            try:
                stack.set(ComponentRights.from_dict({**entry, "component": key}))
            except ValueError:
                logger.warning("unknown rights component %r in stored stack; skipped", key)
        return stack


def intersect(stack: RightsStack) -> LatticeResult:
    """
    Effective rights = the intersection of every applicable component.

    Three rules, in order:

    1. **Any blocking component blocks everything.** An in-copyright work
       is not freed by a CC0 scan of it.
    2. **A permission survives only if every component grants it.** This
       is what makes widening structurally impossible.
    3. **The scope is the narrowest any component imposes.** US-only
       anywhere in the stack means US-only overall.

    A stack whose only stated components are SCAN, OCR, or METADATA
    yields nothing: those layers describe the packaging, not the work,
    and treating them as sufficient is precisely the confusion that
    quarantines exist to catch.
    """
    applicable = stack.applicable()

    if not applicable:
        return LatticeResult(
            commercial_use=False, redistribution=False, derivatives=False,
            attribution_required=False, share_alike=False,
            scope=DistributionScope.UNKNOWN, effective_license=LIC_UNKNOWN,
            components=tuple(stack.components.values()),
            reason_codes=(ReasonCode.NO_LICENSE_STATEMENT,),
            blocked=True,
            notes="no component states a licence governing the content",
        )

    substantive = [c for c in applicable if c.component not in INSUFFICIENT_ALONE]
    if not substantive:
        stated = ", ".join(sorted(c.component.value for c in applicable))
        return LatticeResult(
            commercial_use=False, redistribution=False, derivatives=False,
            attribution_required=False, share_alike=False,
            scope=DistributionScope.UNKNOWN, effective_license=LIC_UNKNOWN,
            components=tuple(stack.components.values()),
            reason_codes=(ReasonCode.METADATA_LICENSE_ONLY,),
            blocked=True,
            notes=(
                f"only {stated} carries a licence; a free scan or OCR layer over a work "
                f"whose own status is unstated establishes nothing about the work"
            ),
        )

    commercial = redistribution = derivatives = True
    attribution = share_alike = False
    scope_rank = _SCOPE_RANK[DistributionScope.WORLDWIDE]
    limiting: list[RightsComponent] = []
    reason_codes: list[ReasonCode] = []
    blocked = False
    unknown_components: list[RightsComponent] = []

    for component_rights in applicable:
        facts = facts_for(component_rights.normalized_license)

        if component_rights.normalized_license == LIC_UNKNOWN:
            unknown_components.append(component_rights.component)
            blocked = True
            if ReasonCode.UNKNOWN_LICENSE not in reason_codes:
                reason_codes.append(ReasonCode.UNKNOWN_LICENSE)
            limiting.append(component_rights.component)
            continue

        # Rule 1: a component that grants no redistribution blocks the
        # whole document, whatever the other layers say.
        if not facts.redistribution:
            blocked = True
            limiting.append(component_rights.component)

        # Rule 2: intersection of permissions.
        before = (commercial, redistribution, derivatives)
        commercial = commercial and facts.commercial_use
        redistribution = redistribution and facts.redistribution
        derivatives = derivatives and facts.derivatives
        if (commercial, redistribution, derivatives) != before:
            limiting.append(component_rights.component)

        # Obligations accumulate rather than intersect: if ANY layer
        # requires attribution, the document requires attribution.
        attribution = attribution or facts.attribution_required
        share_alike = share_alike or facts.share_alike

        # Rule 3: narrowest scope wins.
        component_rank = _SCOPE_RANK[facts.inherent_scope]
        if component_rank < scope_rank:
            scope_rank = component_rank
            if component_rights.component not in limiting:
                limiting.append(component_rights.component)

    scope = _RANK_TO_SCOPE[scope_rank]
    if scope == DistributionScope.LOCAL_US_ONLY and ReasonCode.SCOPE_NOT_ESTABLISHED_WORLDWIDE not in reason_codes:
        reason_codes.append(ReasonCode.SCOPE_NOT_ESTABLISHED_WORLDWIDE)

    effective = _effective_license_name(applicable, blocked)

    notes = ""
    if unknown_components:
        notes = (
            "unstated licence on "
            + ", ".join(sorted(c.value for c in unknown_components))
            + " -- an unstated layer cannot be assumed permissive"
        )

    return LatticeResult(
        commercial_use=commercial and not blocked,
        redistribution=redistribution and not blocked,
        derivatives=derivatives and not blocked,
        attribution_required=attribution,
        share_alike=share_alike,
        scope=scope,
        effective_license=effective,
        components=tuple(stack.components.values()),
        limiting_components=tuple(dict.fromkeys(limiting)),
        reason_codes=tuple(reason_codes),
        blocked=blocked,
        notes=notes,
    )


def _effective_license_name(applicable: list[ComponentRights], blocked: bool) -> str:
    """
    A single name for the effective rights.

    Reported as the most restrictive component's licence, because that is
    the one actually governing what may be done. Naming the most
    permissive would be actively misleading on a mixed stack.
    """
    if blocked:
        restrictive = [c for c in applicable if not facts_for(c.normalized_license).redistribution]
        if restrictive:
            return restrictive[0].normalized_license
        return LIC_UNKNOWN

    ranked = sorted(
        applicable,
        key=lambda c: (
            _SCOPE_RANK[facts_for(c.normalized_license).inherent_scope],
            facts_for(c.normalized_license).commercial_use,
            facts_for(c.normalized_license).derivatives,
            not facts_for(c.normalized_license).attribution_required,
        ),
    )
    return ranked[0].normalized_license if ranked else LIC_UNKNOWN


def stack_from_signal(signal, *, source_text_component: RightsComponent = RightsComponent.SOURCE_TEXT) -> RightsStack:
    """
    Build a one-or-two-layer stack from a Phase I `RightsSignal`.

    The compatibility bridge. Every existing adapter emits a
    `RightsSignal`; this reads its `content_license` as the source text
    and its `metadata_license` as metadata, so the lattice can evaluate a
    Phase I signal without every adapter being rewritten at once.

    The metadata layer is recorded but never contributes permissions,
    which is exactly the Europeana trap the Phase I engine already
    guarded against — now enforced by the component model rather than by
    a special case.
    """
    stack = RightsStack()
    stack.add(
        source_text_component,
        signal.content_license,
        rights_statement_uri=signal.rights_statement_uri,
        evidence=signal.evidence,
    )
    if signal.metadata_license:
        stack.add(RightsComponent.METADATA, signal.metadata_license)
    return stack


#: Licences that grant nothing and never will. Rejected outright rather
#: than quarantined: there is nothing further to establish about them.
_DEFINITIVELY_CLOSED = frozenset(
    {"in-copyright", "all-rights-reserved", "no-copyright-non-commercial-only"}
)

#: Licences that grant something, but never enough for any shipped
#: profile. Also rejected -- a CC BY-NC document will not become
#: acceptable by being looked at again.
_DEFINITIVELY_INCOMPATIBLE = frozenset(
    {"cc-by-nc", "cc-by-nd", "cc-by-nc-sa", "cc-by-nc-nd", "no-copyright-non-commercial-only"}
)


def decide(stack: RightsStack, profile) -> tuple[Decision, LatticeResult]:
    """
    Apply a rights profile to an intersected stack.

    The profile check is unchanged in spirit from Phase I — an allowlist
    of licences and permitted scopes — but it now runs against the
    *effective* rights rather than against whichever licence a regex
    matched first.
    """
    result = intersect(stack)

    if result.blocked:
        if result.effective_license in _DEFINITIVELY_CLOSED:
            return Decision.REJECT, result
        return Decision.QUARANTINE, result

    # NC and ND are not "unresolved" -- they are resolved and
    # incompatible with every shipped profile, and no evidence will ever
    # change that. Holding them for review would be busywork, so they are
    # rejected exactly as the Phase I engine rejects them.
    if result.effective_license in _DEFINITIVELY_INCOMPATIBLE:
        return Decision.REJECT, result

    if result.effective_license not in profile.allowed_licenses:
        return Decision.QUARANTINE, result
    if result.scope not in profile.allowed_scopes:
        return Decision.QUARANTINE, result
    if profile.require_commercial_use and not result.commercial_use:
        return Decision.QUARANTINE, result
    if profile.require_derivatives and not result.derivatives:
        return Decision.QUARANTINE, result

    # The evidence gate, unchanged and non-negotiable: no accept without
    # something recorded to point at.
    if not result.all_evidence():
        return Decision.QUARANTINE, result

    return Decision.ACCEPT, result


__all__ = [
    "CONTENT_COMPONENTS",
    "INSUFFICIENT_ALONE",
    "ComponentRights",
    "LatticeResult",
    "RightsComponent",
    "RightsStack",
    "decide",
    "intersect",
    "stack_from_signal",
]
