"""
The rights engine.

Covers required cases 8-14: licence normalization, refusal of unknown
licences, refusal of NC and ND by default, the metadata/content licence
distinction, the local_research_us and release_worldwide profiles, and
the export guard.

The two properties these tests exist to protect, above all others:

* an accept is impossible without recorded evidence;
* age and an author's death date never, on their own, produce an accept.

Both are asserted directly rather than inferred from behaviour, because
both are the kind of guarantee that erodes silently under refactoring.
"""

from __future__ import annotations

import pytest

from fieldhorizon.corpus.models import Decision, DistributionScope, ReasonCode, RightsSignal
from fieldhorizon.corpus.rights import (
    LIC_ALL_RIGHTS_RESERVED,
    LIC_CC0,
    LIC_CC_BY,
    LIC_CC_BY_NC,
    LIC_CC_BY_ND,
    LIC_CC_BY_SA,
    LIC_COPYRIGHT_NOT_EVALUATED,
    LIC_IN_COPYRIGHT,
    LIC_NO_COPYRIGHT_NC,
    LIC_PDM,
    LIC_PUBLIC_DOMAIN,
    LIC_PUBLIC_DOMAIN_US,
    LIC_UNKNOWN,
    PROFILES,
    evaluate,
    evidence_from_field,
    get_profile,
    normalize_license,
    note_age_inference,
)

US_PROFILE = get_profile("local_research_us")
WORLD_PROFILE = get_profile("release_worldwide")
STRICT_PROFILE = get_profile("strict_public_domain")


def evidence(value: str = "a recorded licence statement"):
    return (evidence_from_field("test:field", value, "https://example.org/rights"),)


# ------------------------------------------------------ 8. normalization


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CC0 1.0 Universal", LIC_CC0),
        ("https://creativecommons.org/publicdomain/zero/1.0/", LIC_CC0),
        ("http://creativecommons.org/publicdomain/mark/1.0/", LIC_PDM),
        ("Public Domain Mark 1.0", LIC_PDM),
        ("https://creativecommons.org/licenses/by/4.0/", LIC_CC_BY),
        ("Creative Commons Attribution 4.0", LIC_CC_BY),
        ("https://creativecommons.org/licenses/by-sa/4.0/", LIC_CC_BY_SA),
        ("CC BY-SA 4.0", LIC_CC_BY_SA),
        ("Attribution-ShareAlike", LIC_CC_BY_SA),
        ("https://creativecommons.org/licenses/by-nc/4.0/", LIC_CC_BY_NC),
        ("https://creativecommons.org/licenses/by-nd/4.0/", LIC_CC_BY_ND),
        ("domaine public", LIC_PUBLIC_DOMAIN),
        ("gemeinfrei", LIC_PUBLIC_DOMAIN),
        ("Public domain in the United States", LIC_PUBLIC_DOMAIN_US),
        ("Public domain in the USA.", LIC_PUBLIC_DOMAIN_US),
        ("All rights reserved", LIC_ALL_RIGHTS_RESERVED),
        ("Tous droits réservés", LIC_ALL_RIGHTS_RESERVED),
        ("In Copyright", LIC_IN_COPYRIGHT),
        ("Copyright Not Evaluated", LIC_COPYRIGHT_NOT_EVALUATED),
        ("Non-Commercial Use Only", LIC_NO_COPYRIGHT_NC),
        ("", LIC_UNKNOWN),
        ("something nobody has ever written on a licence page", LIC_UNKNOWN),
    ],
)
def test_licence_normalization(raw, expected):
    assert normalize_license(raw)[0] == expected


def test_rights_statement_uris_normalize_exactly():
    assert normalize_license("", "http://rightsstatements.org/vocab/InC/1.0/")[0] == LIC_IN_COPYRIGHT
    assert normalize_license("", "http://rightsstatements.org/vocab/CNE/1.0/")[0] == LIC_COPYRIGHT_NOT_EVALUATED
    assert normalize_license("", "http://rightsstatements.org/vocab/NoC-NC/1.0/")[0] == LIC_NO_COPYRIGHT_NC
    assert normalize_license("", "http://rightsstatements.org/vocab/NKC/1.0/")[0] == LIC_PUBLIC_DOMAIN


def test_us_only_public_domain_is_distinguished_from_worldwide():
    """
    The distinction the whole Gutenberg integration rests on. "Public
    domain in the United States" must never normalize to the same value
    as an unqualified public-domain statement.
    """
    us_only, _ = normalize_license("Public domain in the United States")
    worldwide, _ = normalize_license("This work is in the public domain.")

    assert us_only == LIC_PUBLIC_DOMAIN_US
    assert worldwide == LIC_PUBLIC_DOMAIN
    assert us_only != worldwide


# ------------------------------------------- 9. unknown licence refused


def test_unknown_licence_is_quarantined_never_accepted():
    decision = evaluate(
        RightsSignal(content_license="", evidence=evidence()), US_PROFILE
    )
    assert decision.decision == Decision.QUARANTINE
    assert ReasonCode.NO_LICENSE_STATEMENT in decision.reason_codes


def test_a_vague_free_claim_is_not_a_licence():
    for claim in ("free", "free download", "open access", "freely available"):
        decision = evaluate(
            RightsSignal(content_license=claim, evidence=evidence(claim)), US_PROFILE
        )
        assert decision.decision == Decision.QUARANTINE, claim
        assert ReasonCode.VAGUE_FREE_CLAIM in decision.reason_codes, claim


def test_copyright_not_evaluated_is_quarantined():
    decision = evaluate(
        RightsSignal(
            content_license="Copyright Not Evaluated",
            rights_statement_uri="http://rightsstatements.org/vocab/CNE/1.0/",
            evidence=evidence(),
        ),
        US_PROFILE,
    )
    assert decision.decision == Decision.QUARANTINE
    assert ReasonCode.COPYRIGHT_NOT_EVALUATED in decision.reason_codes


def test_in_copyright_and_all_rights_reserved_are_rejected_outright():
    """
    Rejected rather than quarantined: there is nothing further to
    establish about them, so holding them for review would be busywork.
    """
    for text in ("In Copyright", "All rights reserved"):
        decision = evaluate(
            RightsSignal(content_license=text, evidence=evidence(text)), US_PROFILE
        )
        assert decision.decision == Decision.REJECT, text


# ------------------------------------------------ 10. NC and ND refused


@pytest.mark.parametrize(
    "licence",
    [
        "https://creativecommons.org/licenses/by-nc/4.0/",
        "https://creativecommons.org/licenses/by-nd/4.0/",
        "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        "https://creativecommons.org/licenses/by-nc-nd/4.0/",
        "No Copyright - Non-Commercial Use Only",
    ],
)
def test_nc_and_nd_licences_are_refused_under_every_shipped_profile(licence):
    for profile in PROFILES.values():
        decision = evaluate(
            RightsSignal(content_license=licence, evidence=evidence(licence)), profile
        )
        assert decision.decision != Decision.ACCEPT, f"{licence} under {profile.name}"


def test_nc_and_nd_refusals_name_the_specific_restriction():
    nc = evaluate(
        RightsSignal(
            content_license="https://creativecommons.org/licenses/by-nc/4.0/", evidence=evidence()
        ),
        US_PROFILE,
    )
    assert ReasonCode.NON_COMMERCIAL_RESTRICTION in nc.reason_codes

    nd = evaluate(
        RightsSignal(
            content_license="https://creativecommons.org/licenses/by-nd/4.0/", evidence=evidence()
        ),
        US_PROFILE,
    )
    assert ReasonCode.NO_DERIVATIVES_RESTRICTION in nd.reason_codes


# ------------------------------- 11. metadata licence != content licence


def test_open_metadata_licence_with_no_content_licence_is_quarantined():
    """
    The Europeana trap. CC0 metadata says nothing about the object, and
    treating it as permission is the single most common way an
    open-looking record turns out to license nothing at all.
    """
    decision = evaluate(
        RightsSignal(
            content_license="",
            metadata_license="CC0 1.0 Universal (Europeana metadata only)",
            evidence=evidence("metadata is CC0"),
        ),
        US_PROFILE,
    )
    assert decision.decision == Decision.QUARANTINE
    assert ReasonCode.METADATA_LICENSE_ONLY in decision.reason_codes


def test_a_real_content_licence_is_not_undermined_by_the_metadata_licence():
    decision = evaluate(
        RightsSignal(
            content_license="http://creativecommons.org/publicdomain/mark/1.0/",
            metadata_license="CC0 1.0 Universal (Europeana metadata only)",
            evidence=evidence(),
        ),
        US_PROFILE,
    )
    assert decision.decision == Decision.ACCEPT
    assert decision.normalized_license == LIC_PDM


def test_cc0_metadata_alongside_an_in_copyright_object_is_not_a_contradiction():
    """
    That combination is the normal, correct state of an aggregator
    record. Treating it as a conflict would quarantine most of Europeana
    for no reason -- so contradiction detection compares content-level
    statements only.
    """
    decision = evaluate(
        RightsSignal(
            content_license="",
            metadata_license="CC0",
            rights_statement_uri="http://rightsstatements.org/vocab/InC/1.0/",
            evidence=evidence(),
        ),
        US_PROFILE,
    )
    assert decision.decision == Decision.REJECT
    assert ReasonCode.CONTRADICTORY_METADATA not in decision.reason_codes


def test_genuinely_contradictory_content_statements_are_quarantined():
    decision = evaluate(
        RightsSignal(
            content_license="This work is in the public domain.",
            rights_statement_uri="http://rightsstatements.org/vocab/InC/1.0/",
            evidence=evidence(),
        ),
        US_PROFILE,
    )
    assert decision.decision == Decision.QUARANTINE
    assert ReasonCode.CONTRADICTORY_METADATA in decision.reason_codes


# -------------------------------------------- 12. local_research_us


def test_local_research_us_accepts_us_public_domain_and_marks_the_scope():
    decision = evaluate(
        RightsSignal(
            content_license="Public domain in the USA.",
            provider_declared_scope="LOCAL_US_ONLY",
            jurisdictions=("US",),
            evidence=evidence("Public domain in the USA."),
        ),
        US_PROFILE,
    )
    assert decision.decision == Decision.ACCEPT
    assert decision.rights_scope == DistributionScope.LOCAL_US_ONLY
    assert decision.policy_profile == "local_research_us"


def test_local_research_us_accepts_cc0_cc_by_and_cc_by_sa():
    for licence, expected in (
        ("CC0 1.0 Universal", LIC_CC0),
        ("https://creativecommons.org/licenses/by/4.0/", LIC_CC_BY),
        ("https://creativecommons.org/licenses/by-sa/4.0/", LIC_CC_BY_SA),
    ):
        decision = evaluate(
            RightsSignal(content_license=licence, evidence=evidence(licence)), US_PROFILE
        )
        assert decision.decision == Decision.ACCEPT, licence
        assert decision.normalized_license == expected


def test_attribution_and_share_alike_obligations_are_carried_forward():
    decision = evaluate(
        RightsSignal(
            content_license="https://creativecommons.org/licenses/by-sa/4.0/", evidence=evidence()
        ),
        US_PROFILE,
    )
    assert decision.attribution_required is True
    assert decision.share_alike is True

    cc0 = evaluate(RightsSignal(content_license="CC0", evidence=evidence()), US_PROFILE)
    assert cc0.attribution_required is False
    assert cc0.share_alike is False


# ------------------------------------------- 13. release_worldwide


def test_release_worldwide_quarantines_us_only_public_domain():
    """
    The rule that makes the whole LOCAL_US_ONLY scope meaningful: a
    Gutenberg text is usable locally and is NOT worldwide public domain.
    """
    signal = RightsSignal(
        content_license="Public domain in the USA.",
        jurisdictions=("US",),
        evidence=evidence("Public domain in the USA."),
    )

    assert evaluate(signal, US_PROFILE).decision == Decision.ACCEPT
    world = evaluate(signal, WORLD_PROFILE)
    assert world.decision == Decision.QUARANTINE
    assert ReasonCode.NOT_PERMITTED_BY_PROFILE in world.reason_codes


def test_release_worldwide_accepts_licences_that_establish_worldwide_scope():
    for licence in (
        "CC0 1.0 Universal",
        "https://creativecommons.org/licenses/by/4.0/",
        "http://creativecommons.org/publicdomain/mark/1.0/",
    ):
        decision = evaluate(
            RightsSignal(content_license=licence, evidence=evidence(licence)), WORLD_PROFILE
        )
        assert decision.decision == Decision.ACCEPT, licence
        assert decision.rights_scope == DistributionScope.WORLDWIDE


def test_strict_public_domain_refuses_even_attribution_bearing_open_licences():
    decision = evaluate(
        RightsSignal(
            content_license="https://creativecommons.org/licenses/by/4.0/", evidence=evidence()
        ),
        STRICT_PROFILE,
    )
    assert decision.decision == Decision.QUARANTINE
    assert ReasonCode.NOT_PERMITTED_BY_PROFILE in decision.reason_codes

    cc0 = evaluate(RightsSignal(content_license="CC0", evidence=evidence()), STRICT_PROFILE)
    assert cc0.decision == Decision.ACCEPT


# ------------------------------------------ evidence and inference rules


def test_no_accept_is_possible_without_recorded_evidence():
    """
    The load-bearing guarantee. Every other condition passes here; only
    the evidence is missing, and that alone must prevent an accept.
    """
    decision = evaluate(
        RightsSignal(content_license="CC0 1.0 Universal", evidence=()), US_PROFILE
    )
    assert decision.decision == Decision.QUARANTINE
    assert ReasonCode.EVIDENCE_MISSING in decision.reason_codes


def test_evidence_is_hashed_so_a_later_change_is_detectable():
    first = evidence_from_field("test:field", "Public domain", "https://example.org")
    same = evidence_from_field("test:field", "Public domain", "https://example.org")
    changed = evidence_from_field("test:field", "In copyright", "https://example.org")

    assert first.content_hash == same.content_hash
    assert first.content_hash != changed.content_hash


def test_age_and_author_death_date_never_produce_an_accept():
    """
    Required rule 9. A 1650 publication date and an author dead since
    1700 are enriching context and nothing more -- the term of copyright
    depends on jurisdiction, edition, translation, and editorial
    apparatus, none of which a year can settle.
    """
    decision = evaluate(
        RightsSignal(
            content_license="",
            raw_rights_text="Published 1650; author died 1700",
            evidence=evidence("published 1650"),
        ),
        US_PROFILE,
    )
    assert decision.decision != Decision.ACCEPT

    codes = note_age_inference(publication_year=1650, author_death_year=1700)
    assert ReasonCode.AGE_BASED_INFERENCE_ONLY in codes
    assert ReasonCode.AUTHOR_DEATH_INFERENCE_ONLY in codes


def test_access_restrictions_short_circuit_before_any_licence_is_considered():
    borrow = evaluate(
        RightsSignal(content_license="CC0", borrow_only=True, evidence=evidence()), US_PROFILE
    )
    assert borrow.decision == Decision.REJECT
    assert ReasonCode.BORROW_ONLY in borrow.reason_codes

    restricted = evaluate(
        RightsSignal(content_license="CC0", access_restricted=True, evidence=evidence()), US_PROFILE
    )
    assert restricted.decision == Decision.REJECT
    assert ReasonCode.ACCESS_RESTRICTED in restricted.reason_codes


def test_an_untrusted_source_cannot_accept_on_its_own_assertion():
    """The Internet Archive lever."""
    signal = RightsSignal(content_license="CC0 1.0 Universal", evidence=evidence())

    trusted = evaluate(signal, US_PROFILE, source_trusted_for_content_rights=True)
    assert trusted.decision == Decision.ACCEPT

    untrusted = evaluate(signal, US_PROFILE, source_trusted_for_content_rights=False)
    assert untrusted.decision == Decision.QUARANTINE
    assert ReasonCode.UNTRUSTED_UPLOADER_ASSERTION in untrusted.reason_codes


def test_an_anonymous_uploader_assertion_is_quarantined_even_on_a_trusted_source():
    decision = evaluate(
        RightsSignal(content_license="CC0", uploader_asserted_only=True, evidence=evidence()),
        US_PROFILE,
    )
    assert decision.decision == Decision.QUARANTINE
    assert ReasonCode.UNTRUSTED_UPLOADER_ASSERTION in decision.reason_codes


def test_refusals_still_carry_their_evidence():
    """
    Knowing WHAT the provider said is what makes a quarantine reviewable
    later. A refusal that discarded its evidence would be unauditable.
    """
    decision = evaluate(
        RightsSignal(content_license="In Copyright", evidence=evidence("In Copyright")),
        US_PROFILE,
    )
    assert decision.decision == Decision.REJECT
    assert len(decision.evidence) == 1
    assert decision.evidence[0].value == "In Copyright"


def test_a_decision_serializes_to_the_documented_shape():
    decision = evaluate(
        RightsSignal(
            content_license="https://creativecommons.org/licenses/by-sa/4.0/",
            jurisdictions=("FR",),
            evidence=evidence(),
        ),
        US_PROFILE,
    )
    payload = decision.to_dict()

    assert set(payload) == {
        "decision", "normalized_license", "rights_scope", "commercial_use", "redistribution",
        "derivatives", "attribution_required", "share_alike", "jurisdictions", "evidence",
        "reason_codes", "policy_profile", "policy_version", "evidence_checked_at", "notes",
    }
    assert payload["decision"] == "accept"
    assert payload["jurisdictions"] == ["FR"]
    assert isinstance(payload["reason_codes"], list)


def test_unknown_profile_name_is_a_clear_error():
    with pytest.raises(ValueError, match="Unknown rights profile"):
        get_profile("permissive_anything_goes")
