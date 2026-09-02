"""
Gallica: the institutional text pipeline, and the wall it currently hits.

The SRU and Pagination fixtures here were captured from the live service
on 2026-08-01 rather than written from documentation — Phase I's most
expensive lesson was that fixtures written from docs disagree with
reality in ways that only surface during a live run.

The central fact this suite encodes: Gallica's `.texteBrut` redirects to
an **altcha proof-of-work challenge**. Solving it would be bypassing an
anti-bot protection, which this project does not do. Gallica is therefore
a discovery, metadata, and rights-evidence provider that does not declare
content hosting — the exact situation the capability graph was built to
express, and one Phase I could only have recorded as a few hundred
download failures.
"""

from __future__ import annotations

import pytest

from fieldhorizon.corpus.adapters.gallica_text import (
    GALLICA_CAPABILITIES,
    GallicaContentBlocked,
    GallicaTextAdapter,
    candidate_from_record,
    looks_like_the_anti_bot_challenge,
    parse_alto,
    parse_gallica_sru_record,
    parse_pagination,
    reconstruct_pages,
    rights_stack_for,
)
from fieldhorizon.corpus.adapters.sru import parse_sru_envelope
from fieldhorizon.corpus.capabilities import Capability, PlanStatus, ProviderRegistry, build_plan
from fieldhorizon.corpus.lattice import RightsComponent, decide, intersect
from fieldhorizon.corpus.models import Decision, DistributionScope
from fieldhorizon.corpus.rights import get_profile
from tests.corpus_helpers import FIXTURES, fixture_bytes, make_fetcher, make_source

US = get_profile("local_research_us")
WORLD = get_profile("release_worldwide")

SRU_URL = (
    "https://gallica.bnf.fr/SRU?operation=searchRetrieve&version=1.2"
    "&query=test&startRecord=1&maximumRecords=20"
)


def records():
    envelope, _, _ = parse_sru_envelope(fixture_bytes("gallica_sru_text.xml"))
    return [parse_gallica_sru_record(r) for r in envelope]


# ------------------------------------------------------------ SRU parsing


def test_the_live_sru_response_parses():
    """Against the response the real service returned, not an invented one."""
    parsed = [r for r in records() if r is not None]

    assert parsed, "the captured SRU response should contain records"
    for record in parsed:
        assert record.ark.startswith("ark:/")
        assert record.title
        assert record.language == "fr"


def test_public_domain_records_are_recognised():
    parsed = [r for r in records() if r is not None]
    assert any(r.is_public_domain for r in parsed)
    for record in parsed:
        if record.is_public_domain:
            assert "domaine public" in record.rights_text.lower()


def test_printed_monographs_are_recognised_as_text():
    """
    The live catalogue returns `dc:type` of "text" and "monographie
    imprimée" for books.
    """
    parsed = [r for r in records() if r is not None]
    assert any(r.is_text for r in parsed)


def test_an_engraving_is_not_harvested_as_a_book():
    """
    Gallica's catalogue is full of prints, maps, and photographs -- the
    first hit for a plain author search is routinely an engraving.
    Harvesting one as a "book" would be nonsense, so non-text records are
    dropped rather than downloaded and then puzzled over.
    """
    envelope, _, _ = parse_sru_envelope(fixture_bytes("gallica_sru.xml"))
    from fieldhorizon.corpus.adapters.gallica_text import _TEXT_TYPES

    # The Phase I fixture's records are monographs; construct the
    # negative case explicitly against the type vocabulary.
    assert "estampe" not in _TEXT_TYPES
    assert "monographie" in _TEXT_TYPES


def test_a_record_without_an_ark_is_dropped():
    """
    The ARK is Gallica's stable identifier. Without it there is neither
    an idempotent key nor a way to address the object.
    """
    envelope, _, _ = parse_sru_envelope(fixture_bytes("gallica_sru.xml"))
    parsed = [parse_gallica_sru_record(r) for r in envelope]
    assert any(p is None for p in parsed)


def test_an_access_only_statement_is_not_a_licence():
    """
    "Consultable en ligne" means you may look at it on Gallica. It is not
    permission to harvest it, and Phase I already learned to keep the two
    apart.
    """
    envelope, _, _ = parse_sru_envelope(fixture_bytes("gallica_sru.xml"))
    parsed = [p for p in (parse_gallica_sru_record(r) for r in envelope) if p]
    restricted = [p for p in parsed if p.access_restricted]

    assert restricted, "the Phase I fixture contains an access-restricted record"
    for record in restricted:
        assert not record.is_public_domain


# ----------------------------------------------------------- Pagination


def test_the_pagination_service_reports_text_availability():
    """
    `hasContent` is what distinguishes "the institution already has text"
    from "OCR would be needed", which is the difference between a
    harvestable document and an OCR_PENDING one.
    """
    result = parse_pagination(fixture_bytes("gallica_pagination.xml"))

    assert result["ark"] == "bpt6k5619759j"
    assert result["has_content"] is True
    assert result["view_count"] == 544
    assert result["pages"]
    assert all("ordre" in page for page in result["pages"])


# ----------------------------------------------------------------- ALTO


def test_alto_text_comes_from_content_attributes_not_text_nodes():
    """
    ALTO puts its words in `CONTENT` attributes and has no text nodes at
    all, so a naive XML text walk returns an empty string -- a silent
    nothing that looks exactly like a working parser.
    """
    text = parse_alto(fixture_bytes("gallica_alto_page.xml"))

    assert "DÉCLARATION DES DROITS" in text
    assert "Les hommes naissent libres" in text
    assert "et demeurent égaux en droits." in text


def test_alto_preserves_line_and_block_structure():
    """
    A page flattened into one run of words loses the paragraph structure
    the classifier's structural signal depends on.
    """
    text = parse_alto(fixture_bytes("gallica_alto_page.xml"))

    assert "\n\n" in text, "text blocks should be separated"
    heading, body = text.split("\n\n", 1)
    assert heading == "DÉCLARATION DES DROITS"
    assert body.count("\n") >= 1, "lines within a block should be preserved"


def test_an_illustration_block_produces_no_stray_empty_block():
    text = parse_alto(fixture_bytes("gallica_alto_page.xml"))
    assert not text.endswith("\n\n")
    assert "\n\n\n" not in text


def test_pages_reconstruct_without_injecting_page_markers():
    """
    A marker on every page would become the most repeated line in the
    document, and the boilerplate stripper would spend its effort on it.
    """
    combined = reconstruct_pages(["page one text", "", "page two text", "   "])

    assert combined == "page one text\n\npage two text"
    assert "page 1" not in combined.lower()


def test_alto_parsing_is_hardened_like_every_other_xml_path():
    from fieldhorizon.corpus.security import UnsafeXMLError

    with pytest.raises(UnsafeXMLError):
        parse_alto(fixture_bytes("malicious_xxe.xml"))


# ------------------------------------------------- the anti-bot challenge


def test_the_altcha_challenge_is_detected():
    """
    Detected so the harvester stops cleanly and records ACCESS_BLOCKED.
    There is deliberately no code path that attempts the challenge.
    """
    body = fixture_bytes("gallica_altcha_challenge.html")

    assert looks_like_the_anti_bot_challenge(
        "https://gallica.bnf.fr/services/engine/search/altcha?altchaNotVerified=false", b""
    )
    assert looks_like_the_anti_bot_challenge("", body)
    assert not looks_like_the_anti_bot_challenge(
        "https://gallica.bnf.fr/ark:/12148/x.texteBrut", b"<html><body>real content</body></html>"
    )


def test_fetching_gallica_content_refuses_with_a_reason():
    """
    The refusal is explicit in the code rather than implicit in a
    failure. An operator reading this exception learns why, not merely
    that.
    """
    adapter = GallicaTextAdapter(
        make_source("gallica", "gallica_text", base_url="https://gallica.bnf.fr/SRU",
                    allowed_hosts=["gallica.bnf.fr"]),
        make_fetcher({}),
    )
    record = next(r for r in records() if r is not None)
    candidate = candidate_from_record(record, "gallica")

    with pytest.raises(GallicaContentBlocked, match="anti-bot challenge"):
        adapter.fetch_content(candidate)


def test_gallica_does_not_declare_a_capability_it_may_not_exercise():
    """
    Declaring CONTENT_HOSTING would make the planner build plans that
    cannot succeed, and turn a provider's legitimate refusal into our
    repeated failures.
    """
    assert GALLICA_CAPABILITIES.has(Capability.DISCOVERY)
    assert GALLICA_CAPABILITIES.has(Capability.METADATA)
    assert GALLICA_CAPABILITIES.has(Capability.RIGHTS_EVIDENCE)

    assert not GALLICA_CAPABILITIES.has(Capability.CONTENT_HOSTING)
    assert not GALLICA_CAPABILITIES.has(Capability.RENDERED_TEXT)
    assert "altcha" in GALLICA_CAPABILITIES.notes


def test_a_gallica_document_plans_as_metadata_only_not_as_a_failure():
    """
    The whole point of Phase II. Phase I would have recorded a download
    failure; this is a complete metadata acquisition with full rights
    evidence and no available host.
    """
    registry = ProviderRegistry()
    registry.register(GALLICA_CAPABILITIES, credential_available=False)

    plan = build_plan(
        "gallica:ark:/12148/bpt6k5619759j",
        registry,
        discovered_by="gallica",
        metadata_from="gallica",
        rights_evidence_from="gallica",
    )

    assert plan.status == PlanStatus.METADATA_ONLY
    assert plan.is_error() is False
    assert plan.rights_evidence_from == "gallica"


# ------------------------------------------------------- rights lattice


def test_a_public_domain_record_states_work_source_text_and_scan_separately():
    """
    Gallica distinguishes the rights of the WORK, the SCAN, the OCR TEXT,
    and the METADATA. Stating them as separate layers is what lets a
    future change to the scan's terms show up as a scan-level change
    rather than silently altering the work's status.
    """
    record = next(r for r in records() if r is not None and r.is_public_domain)
    stack = rights_stack_for(record)

    assert stack.get(RightsComponent.WORK) is not None
    assert stack.get(RightsComponent.SOURCE_TEXT) is not None
    assert stack.get(RightsComponent.SCAN) is not None

    result = intersect(stack)
    assert not result.blocked
    assert result.scope == DistributionScope.WORLDWIDE
    assert decide(stack, US)[0] == Decision.ACCEPT


def test_a_record_without_an_affirmative_statement_is_quarantined():
    """Not stated is not granted."""
    from fieldhorizon.corpus.adapters.gallica_text import GallicaRecord

    record = GallicaRecord(
        ark="ark:/12148/restricted", title="Un ouvrage consultable", authors=(),
        language="fr", date="1952", document_type="monographie", subjects=(),
        rights_text="Consultable en ligne", is_public_domain=False,
        access_restricted=True, is_text=True, total_views=200,
        canonical_url="https://gallica.bnf.fr/ark:/12148/restricted",
    )
    stack = rights_stack_for(record)
    decision, result = decide(stack, US)

    assert decision == Decision.QUARANTINE
    assert result.blocked
    assert "no affirmative public-domain statement" in (
        stack.get(RightsComponent.SOURCE_TEXT).notes
    )


def test_the_date_is_never_used_as_rights_evidence():
    """
    A 1750 publication date is context, never proof: an edition, a
    translation, or an editorial apparatus can each carry their own term.
    """
    from fieldhorizon.corpus.adapters.gallica_text import GallicaRecord

    ancient_but_unstated = GallicaRecord(
        ark="ark:/12148/old", title="Un livre de 1650", authors=("Anonyme",),
        language="fr", date="1650", document_type="monographie", subjects=(),
        rights_text="", is_public_domain=False, access_restricted=False,
        is_text=True, total_views=300,
        canonical_url="https://gallica.bnf.fr/ark:/12148/old",
    )

    assert decide(rights_stack_for(ancient_but_unstated), US)[0] == Decision.QUARANTINE


def test_gallica_rights_evidence_is_recorded_with_somewhere_to_point():
    record = next(r for r in records() if r is not None and r.is_public_domain)
    candidate = candidate_from_record(record, "gallica")

    kinds = {e.kind for e in candidate.rights.evidence}
    assert "gallica:dc:rights" in kinds
    assert "gallica:reuse-conditions" in kinds
    for item in candidate.rights.evidence:
        assert item.url
        assert item.content_hash


# ---------------------------------------------------------------- adapter


def test_discovery_walks_the_catalogue_offline():
    fetcher = make_fetcher({SRU_URL: FIXTURES / "gallica_sru_text.xml"})
    source = make_source(
        "gallica", "gallica_text",
        base_url="https://gallica.bnf.fr/SRU",
        allowed_hosts=["gallica.bnf.fr"],
        options={"query": "test", "page_size": 20},
    )

    result = GallicaTextAdapter(source, fetcher).discover_page(None)

    assert result.candidates
    for candidate in result.candidates:
        assert candidate.external_id.startswith("ark:/")
        # No download URL: naming one would produce a plan that cannot
        # succeed and a failure that is not ours.
        assert candidate.download_url == ""
        assert candidate.raw_metadata["content_route"] == "blocked_by_anti_bot_challenge"


def test_candidates_carry_their_rights_components_for_the_database():
    fetcher = make_fetcher({SRU_URL: FIXTURES / "gallica_sru_text.xml"})
    source = make_source(
        "gallica", "gallica_text", base_url="https://gallica.bnf.fr/SRU",
        allowed_hosts=["gallica.bnf.fr"], options={"query": "test", "page_size": 20},
    )
    candidate = GallicaTextAdapter(source, fetcher).discover_page(None).candidates[0]

    components = candidate.raw_metadata["rights_components"]
    assert "source_text" in components
    assert components["source_text"]["normalized_license"]


def test_the_adapter_never_reaches_the_network_in_tests():
    from fieldhorizon.corpus.http import FetchError

    adapter = GallicaTextAdapter(
        make_source("gallica", "gallica_text", base_url="https://gallica.bnf.fr/SRU",
                    allowed_hosts=["gallica.bnf.fr"]),
        make_fetcher({}),
    )
    with pytest.raises(FetchError, match="no fixture"):
        adapter.discover_page(None)
