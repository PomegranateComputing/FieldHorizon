"""
The rights lattice.

Phase I adjudicated one licence per document and took the first pattern
that matched. The two most consequential defects the live pilot found
were both instances of that, and Phase I fixed them by *reordering
regexes* -- making the ordering in `rights._TEXT_PATTERNS` load-bearing
and forcing the documentation to carry a standing warning about it.

These tests exist so that warning is no longer the safety mechanism. The
lattice cannot widen rights because intersection has no way to widen
anything, and every case below asserts that structurally rather than by
checking that a particular regex still sorts first.

The nine cases the Phase II brief requires are each marked.
"""

from __future__ import annotations

import pytest

from fieldhorizon.corpus.lattice import (
    CONTENT_COMPONENTS,
    INSUFFICIENT_ALONE,
    ComponentRights,
    RightsComponent,
    RightsStack,
    decide,
    intersect,
    stack_from_signal,
)
from fieldhorizon.corpus.models import Decision, DistributionScope, Evidence, ReasonCode, RightsSignal
from fieldhorizon.corpus.rights import (
    LIC_CC0,
    LIC_CC_BY,
    LIC_CC_BY_SA,
    LIC_IN_COPYRIGHT,
    LIC_PUBLIC_DOMAIN,
    LIC_PUBLIC_DOMAIN_US,
    LIC_UNKNOWN,
    get_profile,
)

US = get_profile("local_research_us")
WORLD = get_profile("release_worldwide")
STRICT = get_profile("strict_public_domain")


def evidence(value: str = "a recorded statement") -> tuple[Evidence, ...]:
    return (Evidence(kind="test", value=value, url="https://example.org/rights"),)


def stack(**components: str) -> RightsStack:
    """`stack(source_text="CC0", scan="CC BY")` with evidence attached."""
    s = RightsStack()
    for name, licence in components.items():
        s.add(RightsComponent(name), licence, evidence=evidence(licence))
    return s


# ------------------------------------------------ 1. PD-US work + CC0 edition


def test_pd_us_work_with_a_cc0_edition_stays_us_only():
    """
    **The mandatory case.** Standard Ebooks states the work is public
    domain in the United States and separately dedicates its own
    editorial contribution under CC0.

    Phase I read the CC0 first and produced a worldwide grant -- turning
    an explicit "check your local laws" into permission to redistribute
    globally. This must be LOCAL_US_ONLY, and it must be so because of
    intersection, not because of pattern ordering.
    """
    result = intersect(
        stack(
            source_text="Public domain in the United States.",
            editorial_contribution="CC0 1.0 Universal",
        )
    )

    assert result.scope == DistributionScope.LOCAL_US_ONLY
    assert result.effective_license == LIC_PUBLIC_DOMAIN_US
    assert RightsComponent.SOURCE_TEXT in result.limiting_components
    assert ReasonCode.SCOPE_NOT_ESTABLISHED_WORLDWIDE in result.reason_codes
    assert not result.blocked


def test_the_us_only_result_is_independent_of_component_order():
    """
    The whole point: order must not matter. If it did, we would have
    replaced one ordering dependency with another.
    """
    a = RightsStack()
    a.add(RightsComponent.SOURCE_TEXT, "Public domain in the United States.", evidence=evidence())
    a.add(RightsComponent.EDITORIAL_CONTRIBUTION, "CC0", evidence=evidence())

    b = RightsStack()
    b.add(RightsComponent.EDITORIAL_CONTRIBUTION, "CC0", evidence=evidence())
    b.add(RightsComponent.SOURCE_TEXT, "Public domain in the United States.", evidence=evidence())

    assert intersect(a).scope == intersect(b).scope == DistributionScope.LOCAL_US_ONLY


def test_the_us_only_document_is_accepted_locally_and_quarantined_worldwide():
    s = stack(
        source_text="Public domain in the United States.",
        editorial_contribution="CC0 1.0 Universal",
    )
    assert decide(s, US)[0] == Decision.ACCEPT
    assert decide(s, WORLD)[0] == Decision.QUARANTINE
    assert decide(s, STRICT)[0] == Decision.QUARANTINE


# ------------------------------------- 2. worldwide PD work + CC0 edition


def test_worldwide_public_domain_with_a_cc0_edition_stays_worldwide():
    """
    The control for the case above. When nothing in the stack is
    jurisdiction-limited, the result must NOT be needlessly narrowed --
    a lattice that made everything US-only would be safe and useless.
    """
    result = intersect(
        stack(
            source_text="This work is in the public domain.",
            editorial_contribution="CC0 1.0 Universal",
        )
    )

    assert result.scope == DistributionScope.WORLDWIDE
    assert not result.blocked
    assert result.commercial_use and result.redistribution and result.derivatives

    s = stack(source_text="This work is in the public domain.", editorial_contribution="CC0")
    assert decide(s, WORLD)[0] == Decision.ACCEPT
    assert decide(s, STRICT)[0] == Decision.ACCEPT


# ------------------------------------ 3. CC0 metadata + unknown content


def test_cc0_metadata_over_unknown_content_grants_nothing():
    """
    The Europeana trap. Europeana publishes its metadata under CC0; that
    says nothing whatsoever about the object. Metadata is excluded from
    the content components entirely, so it cannot contribute a single
    permission.
    """
    s = RightsStack()
    s.add(RightsComponent.METADATA, "CC0 1.0 Universal", evidence=evidence())

    result = intersect(s)
    assert result.blocked
    assert result.scope == DistributionScope.UNKNOWN
    assert decide(s, US)[0] == Decision.QUARANTINE


def test_metadata_never_appears_among_the_content_components():
    assert RightsComponent.METADATA not in CONTENT_COMPONENTS
    assert RightsComponent.METADATA in INSUFFICIENT_ALONE


def test_open_metadata_cannot_rescue_an_unstated_source_text():
    s = RightsStack()
    s.add(RightsComponent.METADATA, "CC0", evidence=evidence())
    s.add(RightsComponent.SOURCE_TEXT, "", evidence=evidence())

    result = intersect(s)
    assert result.blocked
    assert ReasonCode.UNKNOWN_LICENSE in result.reason_codes


# --------------------------------------- 4. free scan + protected text


def test_a_freely_licensed_scan_does_not_free_a_protected_work():
    """
    A library may release its scans under CC BY while the book itself is
    in copyright. The scan's generosity is about the photograph, not the
    text.
    """
    result = intersect(
        stack(source_text="In Copyright", scan="https://creativecommons.org/licenses/by/4.0/")
    )

    assert result.blocked
    assert RightsComponent.SOURCE_TEXT in result.limiting_components
    assert decide(
        stack(source_text="In Copyright", scan="CC BY 4.0"), US
    )[0] == Decision.REJECT


def test_a_free_scan_alone_establishes_nothing():
    """No statement about the work at all -- only about the image of it."""
    s = RightsStack()
    s.add(RightsComponent.SCAN, "CC0", evidence=evidence())

    result = intersect(s)
    assert result.blocked
    assert ReasonCode.METADATA_LICENSE_ONLY in result.reason_codes
    assert "establishes nothing about the work" in result.notes


# ----------------------------------------- 5. free OCR + protected work


def test_freely_licensed_ocr_does_not_free_a_protected_work():
    result = intersect(
        stack(source_text="All rights reserved", ocr="CC0 1.0 Universal")
    )

    assert result.blocked
    assert RightsComponent.SOURCE_TEXT in result.limiting_components


def test_ocr_alone_establishes_nothing():
    s = RightsStack()
    s.add(RightsComponent.OCR, "CC BY 4.0", evidence=evidence())

    result = intersect(s)
    assert result.blocked
    assert ReasonCode.METADATA_LICENSE_ONLY in result.reason_codes


def test_ocr_over_a_public_domain_work_is_fine():
    """The positive control: OCR is only insufficient *alone*."""
    result = intersect(
        stack(source_text="This work is in the public domain.", ocr="CC0")
    )
    assert not result.blocked
    assert result.scope == DistributionScope.WORLDWIDE


# ------------------------------------------------------- 6. CC BY content


def test_cc_by_content_carries_its_attribution_obligation():
    result = intersect(stack(source_text="https://creativecommons.org/licenses/by/4.0/"))

    assert not result.blocked
    assert result.effective_license == LIC_CC_BY
    assert result.attribution_required is True
    assert result.share_alike is False
    assert result.commercial_use and result.derivatives
    assert result.scope == DistributionScope.WORLDWIDE

    assert decide(stack(source_text="CC BY 4.0"), US)[0] == Decision.ACCEPT
    assert decide(stack(source_text="CC BY 4.0"), WORLD)[0] == Decision.ACCEPT
    # strict_public_domain refuses any attribution-bearing licence.
    assert decide(stack(source_text="CC BY 4.0"), STRICT)[0] == Decision.QUARANTINE


# ---------------------------------------------------- 7. CC BY-SA content


def test_cc_by_sa_content_carries_both_obligations():
    result = intersect(stack(source_text="https://creativecommons.org/licenses/by-sa/4.0/"))

    assert not result.blocked
    assert result.effective_license == LIC_CC_BY_SA
    assert result.attribution_required is True
    assert result.share_alike is True


def test_obligations_accumulate_rather_than_intersect():
    """
    Permissions intersect; obligations accumulate. If ANY layer demands
    attribution the document owes attribution -- taking the intersection
    of obligations would quietly discharge a duty the licence imposes.
    """
    result = intersect(
        stack(
            source_text="This work is in the public domain.",   # no obligations
            editorial_contribution="CC BY-SA 4.0",              # both obligations
        )
    )

    assert result.attribution_required is True
    assert result.share_alike is True


# -------------------------------------------------- 8. contradictory licences


def test_contradictory_layers_resolve_to_the_restrictive_one():
    """
    Not an error -- a resolution. One layer saying "public domain" and
    another "all rights reserved" is a real state of real catalogues, and
    the safe reading is the restrictive one.
    """
    result = intersect(
        stack(
            source_text="This work is in the public domain.",
            translation="All rights reserved",
        )
    )

    assert result.blocked
    assert RightsComponent.TRANSLATION in result.limiting_components
    assert decide(
        stack(source_text="Public domain", translation="All rights reserved"), US
    )[0] == Decision.REJECT


def test_a_translation_can_be_the_only_thing_standing_in_the_way():
    """
    A public-domain original with an in-copyright translation is the
    single most common trap in a multilingual corpus.
    """
    original_only = intersect(stack(source_text="This work is in the public domain."))
    assert not original_only.blocked

    with_translation = intersect(
        stack(source_text="This work is in the public domain.", translation="In Copyright")
    )
    assert with_translation.blocked


# --------------------------------- 9. a licence disappearing at audit time


def test_a_component_whose_licence_disappears_blocks_the_document():
    """
    An audit re-reads a provider's statement and finds it gone. The
    document must stop qualifying immediately: an unstated layer is never
    assumed permissive.
    """
    before = intersect(
        stack(source_text="CC BY 4.0", editorial_contribution="CC0")
    )
    assert not before.blocked

    after = RightsStack()
    after.add(RightsComponent.SOURCE_TEXT, "", evidence=evidence("statement no longer present"))
    after.add(RightsComponent.EDITORIAL_CONTRIBUTION, "CC0", evidence=evidence())

    result = intersect(after)
    assert result.blocked
    assert ReasonCode.UNKNOWN_LICENSE in result.reason_codes
    assert "cannot be assumed permissive" in result.notes
    assert decide(after, US)[0] == Decision.QUARANTINE


# ----------------------------------------------------- structural guarantees


@pytest.mark.parametrize(
    "secondary_licence",
    ["CC0 1.0 Universal", "https://creativecommons.org/licenses/by/4.0/",
     "Public Domain Mark 1.0", "This work is in the public domain."],
)
def test_no_secondary_component_can_ever_widen_the_primary(secondary_licence):
    """
    The structural claim, asserted across every permissive licence we
    recognise: adding a permissive secondary layer to a US-only work can
    never produce a worldwide result.
    """
    narrow = intersect(stack(source_text="Public domain in the United States."))

    for component in (
        RightsComponent.EDITORIAL_CONTRIBUTION,
        RightsComponent.DIGITAL_EDITION,
        RightsComponent.SCAN,
        RightsComponent.OCR,
    ):
        s = RightsStack()
        s.add(RightsComponent.SOURCE_TEXT, "Public domain in the United States.", evidence=evidence())
        s.add(component, secondary_licence, evidence=evidence())
        widened = intersect(s)

        assert widened.scope == narrow.scope == DistributionScope.LOCAL_US_ONLY
        # Permissions can only ever shrink. `<=` on booleans reads as
        # "was not turned on by the addition", which is the claim.
        assert widened.commercial_use <= narrow.commercial_use
        assert widened.redistribution <= narrow.redistribution
        assert widened.derivatives <= narrow.derivatives


def test_adding_a_component_never_grants_a_permission_that_was_absent():
    """
    Monotonicity: intersection can only ever remove. Adding any layer to
    a stack must not turn a False permission True.
    """
    base = intersect(stack(source_text="CC BY-ND 4.0"))
    assert base.derivatives is False

    s = RightsStack()
    s.add(RightsComponent.SOURCE_TEXT, "CC BY-ND 4.0", evidence=evidence())
    s.add(RightsComponent.EDITORIAL_CONTRIBUTION, "CC0", evidence=evidence())

    assert intersect(s).derivatives is False


def test_an_empty_stack_grants_nothing():
    result = intersect(RightsStack())
    assert result.blocked
    assert result.scope == DistributionScope.UNKNOWN
    assert ReasonCode.NO_LICENSE_STATEMENT in result.reason_codes


def test_a_component_marked_not_applicable_is_skipped():
    """
    "This born-digital text has no scan" is different from "we do not
    know the scan's licence", and only the second should block.
    """
    s = RightsStack()
    s.add(RightsComponent.SOURCE_TEXT, "CC0", evidence=evidence())
    s.set(ComponentRights.absent(RightsComponent.SCAN, "born-digital; no scan exists"))

    result = intersect(s)
    assert not result.blocked
    assert result.scope == DistributionScope.WORLDWIDE


def test_the_evidence_gate_still_applies_through_the_lattice():
    """
    Phase I's non-negotiable rule, preserved: no accept without recorded
    evidence, whatever the components say.
    """
    s = RightsStack()
    s.add(RightsComponent.SOURCE_TEXT, "CC0 1.0 Universal")  # no evidence

    decision, result = decide(s, US)
    assert not result.blocked          # the licence itself is fine...
    assert decision == Decision.QUARANTINE  # ...but nothing backs it


# ------------------------------------------------------ Phase I bridge


def test_a_phase_one_rights_signal_still_evaluates():
    """
    Every existing adapter emits a `RightsSignal`. The bridge must read
    one without each adapter being rewritten first.
    """
    signal = RightsSignal(
        content_license="https://creativecommons.org/licenses/by-sa/4.0/",
        metadata_license="CC0 1.0 Universal",
        evidence=evidence(),
    )
    s = stack_from_signal(signal)

    assert s.get(RightsComponent.SOURCE_TEXT).normalized_license == LIC_CC_BY_SA
    assert s.get(RightsComponent.METADATA).normalized_license == LIC_CC0

    result = intersect(s)
    assert not result.blocked
    assert result.share_alike is True
    assert decide(s, US)[0] == Decision.ACCEPT


def test_the_bridge_preserves_the_metadata_only_trap():
    """A Phase I signal with open metadata and no content licence."""
    signal = RightsSignal(
        content_license="",
        metadata_license="CC0 1.0 Universal (Europeana metadata only)",
        evidence=evidence(),
    )
    decision, result = decide(stack_from_signal(signal), US)

    assert decision == Decision.QUARANTINE
    assert result.blocked


def test_the_lattice_and_the_phase_one_engine_agree_on_simple_cases():
    """
    Where a document genuinely has one layer, the lattice must reach the
    same verdict as the Phase I engine. A rewrite that changed simple
    answers would be a regression dressed as an improvement.
    """
    from fieldhorizon.corpus.rights import evaluate

    for licence in ("CC0 1.0 Universal", "https://creativecommons.org/licenses/by/4.0/",
                    "In Copyright", "All rights reserved",
                    "https://creativecommons.org/licenses/by-nc/4.0/"):
        signal = RightsSignal(content_license=licence, evidence=evidence())
        phase_one = evaluate(signal, US).decision
        phase_two = decide(stack_from_signal(signal), US)[0]
        assert phase_one == phase_two, f"{licence}: {phase_one} vs {phase_two}"


def test_the_lattice_result_serializes_for_the_database():
    result = intersect(
        stack(source_text="Public domain in the United States.", editorial_contribution="CC0")
    )
    payload = result.to_dict()

    assert payload["scope"] == "LOCAL_US_ONLY"
    assert payload["effective_license"] == LIC_PUBLIC_DOMAIN_US
    assert "source_text" in payload["limiting_components"]
    assert isinstance(payload["components"], list)
    assert all("component" in c for c in payload["components"])


def test_unknown_stays_unknown_rather_than_defaulting_open():
    result = intersect(stack(source_text="some words nobody has written on a licence page"))
    assert result.blocked
    assert result.effective_license == LIC_UNKNOWN


def test_public_domain_constants_are_distinguished():
    """Guards the distinction the whole scope model rests on."""
    assert LIC_PUBLIC_DOMAIN != LIC_PUBLIC_DOMAIN_US
    worldwide = intersect(stack(source_text="This work is in the public domain."))
    us_only = intersect(stack(source_text="Public domain in the United States."))
    assert worldwide.scope == DistributionScope.WORLDWIDE
    assert us_only.scope == DistributionScope.LOCAL_US_ONLY


# ------------------------------------------------------ round trip


def test_a_stack_survives_a_round_trip_through_its_serialized_form():
    """
    Found by the Standard Ebooks live pilot, and worth stating plainly:
    a stack rebuilt from `to_dict` **without its evidence** intersects to
    the right licence, the right scope, and the right limiting component
    -- every visible field correct -- and is then quarantined by the
    evidence gate. Seventeen well-documented editions came back
    `quarantine` with nothing in the result to explain it.

    So the round trip is asserted on the DECISION, not on the licence.
    Comparing licences would have passed while the bug was present.
    """
    original = stack(
        source_text="Public domain in the United States.",
        editorial_contribution="CC0 1.0 Universal",
    )
    restored = RightsStack.from_dict(original.to_dict())

    assert decide(original, US)[0] == Decision.ACCEPT
    assert decide(restored, US)[0] == Decision.ACCEPT, "evidence was lost in the round trip"

    before, after = intersect(original), intersect(restored)
    assert after.scope == before.scope
    assert after.effective_license == before.effective_license
    assert after.limiting_components == before.limiting_components
    # As a set: `to_dict` sorts components by name, so evidence comes back
    # in a different order than it went in. That ordering carries no
    # meaning -- what matters is that nothing was dropped.
    assert set(after.all_evidence()) == set(before.all_evidence())


def test_the_round_trip_goes_through_json_not_just_python():
    """
    The database stores JSON text, so the round trip that matters passes
    through `json.dumps`. A dict-to-dict test would not catch a tuple
    that only survives because Python kept the object alive.
    """
    import json

    original = stack(source_text="CC BY-SA 4.0", scan="CC0", translation="Public domain")
    restored = RightsStack.from_dict(json.loads(json.dumps(original.to_dict())))

    assert set(restored.components) == set(original.components)
    for component, rights in original.components.items():
        other = restored.get(component)
        assert other is not None
        assert other.normalized_license == rights.normalized_license
        assert other.evidence == rights.evidence
        assert other.notes == rights.notes


def test_a_stored_component_this_version_does_not_know_is_skipped_not_fatal():
    """
    An older build must stay able to read a database a newer one wrote.
    The unknown layer is dropped and logged rather than crashing -- but
    it IS logged, because a silently dropped rights layer is how a scope
    widens without anyone noticing.
    """
    payload = stack(source_text="CC0").to_dict()
    payload["holographic_projection"] = {"component": "holographic_projection", "evidence": []}

    restored = RightsStack.from_dict(payload)

    assert set(restored.components) == {RightsComponent.SOURCE_TEXT}
    assert decide(restored, WORLD)[0] == Decision.ACCEPT


def test_the_stored_licence_is_not_renormalized_on_read():
    """
    Re-deriving the normalized licence on read would let a change in the
    licence vocabulary silently rewrite a stored verdict. Making a stored
    verdict change is `corpus audit-rights`' job, where it is visible.
    """
    payload = stack(source_text="CC0 1.0 Universal").to_dict()
    payload["source_text"]["normalized_license"] = LIC_IN_COPYRIGHT

    restored = RightsStack.from_dict(payload)

    assert restored.get(RightsComponent.SOURCE_TEXT).normalized_license == LIC_IN_COPYRIGHT
    assert decide(restored, US)[0] != Decision.ACCEPT
