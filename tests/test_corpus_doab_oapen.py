"""
The DOAB -> OAPEN bridge, and the gap it makes visible.

Fixtures captured live on 2026-08-01:

  doab_oai_bridge.xml    4 real DOAB records: 2 pointing at OAPEN, 2 not
  oapen_rest_item.json   a real OAPEN item with its real bitstreams
  doab_robots.txt        DOAB's robots.txt, verbatim

The finding these encode, measured against the live services:

  DOAB OAI-PMH, oai_dc, 100 records      0 carried <dc:rights>
  DOAB REST, 12 items sampled            6 no rights, 5 "open access", 0 a licence
  OAPEN REST, 10 items sampled           0 rights metadata of any kind

So the bridge resolves, the full text is reachable, and every document
still quarantines -- because no provider fills the rights role. That is
the capability graph working, not failing.
"""

from __future__ import annotations

import json

import pytest

from fieldhorizon.corpus.adapters.doab_oapen import (
    DOAB_CAPABILITIES,
    OAPEN_CAPABILITIES,
    Bitstream,
    DoabOapenBridgeAdapter,
    choose_bitstream,
    oapen_handle_from,
    parse_oapen_item,
    rights_stack_for,
)
from fieldhorizon.corpus.capabilities import Capability
from fieldhorizon.corpus.content_signal import ContentSignal, parse_content_signal
from fieldhorizon.corpus.http import FetchError
from fieldhorizon.corpus.lattice import RightsComponent, decide, intersect
from fieldhorizon.corpus.models import ACCESS_BLOCKED_RETRY_AFTER_SECONDS, Decision
from fieldhorizon.corpus.rights import get_profile
from tests.corpus_helpers import FIXTURES, fixture_bytes, fixture_text, make_fetcher, make_source

US = get_profile("local_research_us")

DOAB_OAI = "https://directory.doabooks.org/oai/request"
OAI_URL = f"{DOAB_OAI}?verb=ListRecords&metadataPrefix=oai_dc"
HANDLE = "20.500.12657/96494"
OAPEN_SEARCH = (
    f"https://library.oapen.org/rest/search?query=handle:%22{HANDLE}%22"
    "&expand=metadata,bitstreams"
)


def source(**options):
    return make_source(
        "doab_oapen", "doab_oapen",
        base_url=DOAB_OAI,
        allowed_hosts=["directory.doabooks.org", "library.oapen.org"],
        options=options,
    )


def build_fetcher(**extra):
    mapping = {
        OAI_URL: FIXTURES / "doab_oai_bridge.xml",
        OAPEN_SEARCH: FIXTURES / "oapen_rest_item.json",
    }
    mapping.update(extra)
    return make_fetcher(mapping)


# ------------------------------------------------------- Content-Signal


def test_the_live_doab_robots_declares_conditions_of_use():
    """
    **The finding.** DOAB permits our harvester by its path rules -- we
    match `User-agent: *`, which is `Allow: /`, and we are not one of the
    crawlers it names. In the same file it states that its content may
    not be used to train models, and that AI systems may consume it as
    reference. Those are conditions on USE, not on fetching, so obeying
    them at crawl time and forgetting them afterwards would honour
    nothing at all.
    """
    signal = parse_content_signal(fixture_text("doab_robots.txt"), "directory.doabooks.org")

    assert signal.declared
    assert signal.search is True
    assert signal.ai_train is False
    assert signal.use == "reference"
    assert signal.forbids_training()
    assert set(signal.obligations()) == {"no-ai-training", "use:reference"}


def test_an_unstated_signal_is_neither_permission_nor_refusal():
    """
    The specification is explicit that an absent signal "neither grants
    nor restricts". Collapsing None into True or False destroys the only
    distinction worth recording.
    """
    signal = parse_content_signal(fixture_text("doab_robots.txt"), "directory.doabooks.org")

    assert signal.ai_input is None, "DOAB states nothing about ai-input"
    assert "no-ai-input" not in signal.obligations()


def test_a_signal_addressed_to_another_crawler_is_not_ours_to_read():
    robots = (
        "User-agent: SomeOtherBot\n"
        "Content-Signal: search=no,ai-train=no\n"
        "Disallow: /\n"
        "\n"
        "User-agent: *\n"
        "Allow: /\n"
    )
    signal = parse_content_signal(robots, "example.org")

    assert not signal.declared
    assert signal.obligations() == ()


def test_a_host_with_no_signal_declares_nothing():
    assert not parse_content_signal("User-agent: *\nDisallow: /private\n", "x.org").declared
    assert not parse_content_signal("", "x.org").declared


def test_an_unrecognised_signal_is_kept_rather_than_discarded():
    """A vocabulary that grows must not be silently dropped by an older build."""
    signal = parse_content_signal(
        "User-agent: *\nContent-Signal: search=yes,quantum-use=maybe\n", "x.org"
    )

    assert signal.search is True
    assert signal.unknown == ("quantum-use=maybe",)


def test_the_last_signal_in_the_wildcard_group_wins():
    signal = parse_content_signal(
        "User-agent: *\nContent-Signal: ai-train=yes\nContent-Signal: ai-train=no\n", "x.org"
    )
    assert signal.ai_train is False


def test_the_fetcher_exposes_the_signal_without_a_second_request():
    """
    Parsed from the same robots.txt the fetcher already reads for its
    Disallow rules -- a condition of use should not cost a request.
    """
    from fieldhorizon.corpus.http import BoundedFetcher

    fetcher = BoundedFetcher("test-agent", respect_robots=True)
    fetcher._content_signals["https://directory.doabooks.org"] = parse_content_signal(
        fixture_text("doab_robots.txt"), "directory.doabooks.org"
    )

    signal = fetcher.content_signal_for("https://directory.doabooks.org/oai/request")
    assert signal.forbids_training()


# ---------------------------------------------------------- the bridge


def test_an_oapen_handle_is_found_in_a_doab_identifier():
    assert oapen_handle_from(
        ["ONIX_20241220_9791221503760_289",
         "https://library.oapen.org/handle/20.500.12657/96494"]
    ) == HANDLE
    assert oapen_handle_from(
        ["https://library.oapen.org/bitstream/20.500.12657/96494/1/42376.pdf"]
    ) == ""
    assert oapen_handle_from(["2704-5870", "urn:isbn:123"]) == ""


def test_the_live_oapen_item_parses_with_its_bitstreams():
    metadata, bitstreams = parse_oapen_item(fixture_bytes("oapen_rest_item.json"))

    assert metadata["dc.title"] == ["Experiencing Hektor"]
    assert bitstreams
    assert {b.bundle for b in bitstreams} == {"TEXT", "ORIGINAL", "THUMBNAIL"}


def test_the_pre_extracted_text_layer_is_preferred_over_the_pdf():
    """
    **The capability worth having.** OAPEN publishes the PDF's text layer
    already extracted, as text/plain in a TEXT bundle. Using it means no
    PDF parsing, no guessing whether a scan has a usable text layer, and
    no reaching for OCR at all.
    """
    _, bitstreams = parse_oapen_item(fixture_bytes("oapen_rest_item.json"))
    chosen = choose_bitstream(bitstreams)

    assert chosen is not None
    assert chosen.bundle == "TEXT"
    assert chosen.mime == "text/plain"
    assert chosen.name.endswith(".txt")


def test_the_larger_text_is_taken_when_a_chapter_and_a_book_are_both_attached():
    _, bitstreams = parse_oapen_item(fixture_bytes("oapen_rest_item.json"))
    texts = [b for b in bitstreams if b.bundle == "TEXT"]

    assert len(texts) > 1, "the real item carries two text layers"
    assert choose_bitstream(bitstreams).size == max(b.size for b in texts)


def test_a_thumbnail_is_never_mistaken_for_the_book():
    thumbnail_only = [
        Bitstream("cover.jpg", "THUMBNAIL", "image/jpeg", 16100, "/rest/bitstreams/x/retrieve")
    ]
    assert choose_bitstream(thumbnail_only) is None


def test_an_export_record_is_never_mistaken_for_the_book():
    """A .ris citation is metadata about the book, not the book."""
    exports = [
        Bitstream("x.ris", "EXPORT", "application/octet-stream", 788, "/rest/bitstreams/x/retrieve"),
        Bitstream("x.mar", "EXPORT", "application/octet-stream", 5798, "/rest/bitstreams/y/retrieve"),
    ]
    assert choose_bitstream(exports) is None


def test_an_epub_is_taken_over_a_pdf_when_no_text_layer_exists():
    originals = [
        Bitstream("b.pdf", "ORIGINAL", "application/pdf", 6139991, "/rest/bitstreams/p/retrieve"),
        Bitstream("b.epub", "ORIGINAL", "application/epub+zip", 2306017, "/rest/bitstreams/e/retrieve"),
    ]
    assert choose_bitstream(originals).mime == "application/epub+zip"


def test_a_zero_byte_bitstream_is_not_chosen():
    assert choose_bitstream(
        [Bitstream("empty.txt", "TEXT", "text/plain", 0, "/rest/bitstreams/z/retrieve")]
    ) is None


def test_a_relative_retrieve_link_resolves_against_oapen():
    stream = Bitstream("x.txt", "TEXT", "text/plain", 10, "/rest/bitstreams/abc/retrieve")
    assert stream.url == "https://library.oapen.org/rest/bitstreams/abc/retrieve"


def test_an_empty_oapen_search_result_is_not_an_exception():
    """A DOAB record pointing at a handle that no longer resolves is a
    broken link -- a per-item condition, not a run-ending one."""
    assert parse_oapen_item(b"[]") == ({}, [])


def test_non_json_from_oapen_raises_rather_than_parsing_as_empty():
    with pytest.raises(ValueError, match="non-JSON"):
        parse_oapen_item(b"<html>Just a moment...</html>")


# -------------------------------------------------------------- rights


def test_neither_provider_states_a_licence_so_the_document_quarantines():
    """
    **The measured gap.** The bridge resolves, the full text is reachable,
    and the document is still held -- because nobody published a per-book
    licence. Not a bug; the correct answer to what was actually found.
    """
    stack = rights_stack_for(
        ["open access"], {}, doab_url="https://d/1", oapen_url="https://o/1"
    )

    assert intersect(stack).blocked
    assert decide(stack, US)[0] == Decision.QUARANTINE


def test_open_access_is_recorded_as_an_access_model_not_a_grant():
    """
    "Open access" says the reader pays nothing. It grants no permission
    to redistribute, adapt, or index, and treating it as a licence would
    be exactly the rights-weakening this project forbids.
    """
    for value in ("open access", "info:eu-repo/semantics/openAccess", "Accesso aperto"):
        stack = rights_stack_for([value], {}, doab_url="https://d/1", oapen_url="https://o/1")
        assert stack.get(RightsComponent.WORK).raw_license == "", value


def test_a_real_licence_is_taken_when_one_is_actually_published():
    """The gap is a finding about today's data, not a refusal to read a licence."""
    stack = rights_stack_for(
        ["open access"],
        {"dc.rights.uri": ["https://creativecommons.org/licenses/by/4.0/"]},
        doab_url="https://d/1", oapen_url="https://o/1",
    )
    result = intersect(stack)

    assert not result.blocked
    assert result.attribution_required
    assert decide(stack, US)[0] == Decision.ACCEPT


def test_the_absence_of_a_licence_is_recorded_as_evidence_not_as_silence():
    """
    "We looked at both providers and neither published one" is something
    an operator can act on -- by going to the publisher's page. An empty
    stack is indistinguishable from a parser that never ran.
    """
    stack = rights_stack_for([], {}, doab_url="https://d/1", oapen_url="https://o/1")
    kinds = {e.kind for e in stack.get(RightsComponent.WORK).evidence}

    assert "doab:dc:rights:absent" in kinds
    assert "oapen:dc.rights:absent" in kinds


def test_the_content_signal_travels_with_the_rights_record():
    signal = parse_content_signal(fixture_text("doab_robots.txt"), "directory.doabooks.org")
    stack = rights_stack_for(
        [], {}, doab_url="https://d/1", oapen_url="https://o/1", signal=signal
    )
    evidence = stack.get(RightsComponent.WORK).evidence

    signal_evidence = next(e for e in evidence if e.kind == "doab:robots:content-signal")
    assert "ai-train=no" in signal_evidence.value
    assert "no-ai-training" in signal_evidence.value


# ------------------------------------------------------------- adapter


def test_discovery_bridges_records_that_point_at_oapen():
    fetcher = build_fetcher()
    adapter = DoabOapenBridgeAdapter(source(), fetcher)

    result = adapter.discover_page(None)
    bridged = [c for c in result.candidates if c.raw_metadata.get("content_provider") == "oapen"]

    assert bridged, "the fixture contains records that point at OAPEN"
    for candidate in bridged:
        assert candidate.download_url.startswith("https://library.oapen.org/")
        assert candidate.raw_metadata["pre_extracted_text"] is True
        assert candidate.download_format == "txt"


def test_a_record_with_no_oapen_link_rests_in_metadata_only():
    """
    Two of the four real records point nowhere. That is a complete
    outcome for a directory -- it described a book it does not host --
    and Phase I counted exactly this as a download failure.
    """
    fetcher = build_fetcher()
    adapter = DoabOapenBridgeAdapter(source(), fetcher)

    result = adapter.discover_page(None)
    unhosted = [c for c in result.candidates if not c.raw_metadata.get("content_provider")]

    assert adapter.without_content == 2
    for candidate in unhosted:
        assert candidate.download_url == ""
        assert candidate.rights.access_restricted


def test_a_403_from_oapen_is_recorded_for_retry_and_never_worked_around():
    """
    A 403 is the provider saying no. The only correct responses are to
    wait and to ask again later -- never to ask differently.
    """
    fetcher = build_fetcher()

    def refuse(url, **kwargs):
        if "library.oapen.org" in url:
            raise FetchError(f"403 from {url}", retryable=False, status_code=403)
        return original(url, **kwargs)

    original = fetcher.fetch
    fetcher.fetch = refuse

    adapter = DoabOapenBridgeAdapter(source(), fetcher)
    result = adapter.discover_page(None)

    assert adapter.access_blocked, "the refusal must be recorded"
    blocked = adapter.access_blocked[0]
    assert blocked["retry_after_seconds"] == ACCESS_BLOCKED_RETRY_AFTER_SECONDS
    assert any(c.raw_metadata.get("access_blocked") for c in result.candidates)


def _code_without_prose() -> str:
    """
    The module's executable source, with docstrings and comments removed.

    These assertions are about what the code DOES. Reading the raw source
    would match the module docstring explaining why `/api/search` is off
    limits -- turning a passing rule into a failing test for saying so.
    """
    import ast
    import inspect

    from fieldhorizon.corpus.adapters import doab_oapen

    tree = ast.parse(inspect.getsource(doab_oapen))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
    return ast.unparse(tree)


def test_no_rotating_proxy_or_user_agent_switching_exists_here():
    """A standing rule, asserted rather than merely promised."""
    code = _code_without_prose().lower()
    for forbidden in ("proxies=", "rotating", "user-agent", "cf_clearance", "cloudscraper"):
        assert forbidden not in code, forbidden


def test_the_blocked_api_endpoint_is_never_called():
    """
    DOAB's /api/search answers 403 with a Cloudflare interstitial.
    /rest/search and /oai/request answer normally, and are what this
    talks to.
    """
    assert "/api/search" not in _code_without_prose()


def test_content_resolution_can_be_turned_off_entirely():
    fetcher = build_fetcher()
    adapter = DoabOapenBridgeAdapter(source(resolve_content=False), fetcher)

    result = adapter.discover_page(None)

    assert all(not c.raw_metadata.get("content_provider") for c in result.candidates)
    assert not any("library.oapen.org" in url for url in fetcher.calls)


def test_the_resolution_budget_is_honoured():
    fetcher = build_fetcher()
    adapter = DoabOapenBridgeAdapter(source(max_resolutions=1), fetcher)

    adapter.discover_page(None)

    assert sum(1 for url in fetcher.calls if "library.oapen.org" in url) == 1


def test_the_resumption_token_is_carried_through():
    result = DoabOapenBridgeAdapter(source(), build_fetcher()).discover_page(None)
    assert result.next_cursor == "TOKEN-PAGE-2"
    assert not result.exhausted


# --------------------------------------------------------- capabilities


def test_the_two_providers_declare_different_roles():
    """The whole thesis, in two declarations."""
    assert DOAB_CAPABILITIES.has(Capability.DISCOVERY)
    assert DOAB_CAPABILITIES.has(Capability.METADATA)
    assert not DOAB_CAPABILITIES.has(Capability.CONTENT_HOSTING)

    assert OAPEN_CAPABILITIES.has(Capability.CONTENT_HOSTING)
    assert OAPEN_CAPABILITIES.has(Capability.RENDERED_TEXT)
    assert not OAPEN_CAPABILITIES.has(Capability.DISCOVERY)


def test_neither_provider_claims_to_supply_rights_evidence():
    """
    Measured, not assumed: 100 OAI records, 12 DOAB REST items, and 10
    OAPEN items produced not one machine-readable per-book licence.
    """
    assert not DOAB_CAPABILITIES.has(Capability.RIGHTS_EVIDENCE)
    assert not OAPEN_CAPABILITIES.has(Capability.RIGHTS_EVIDENCE)


def test_the_adapter_makes_no_network_call_in_tests():
    with pytest.raises(FetchError, match="no fixture"):
        DoabOapenBridgeAdapter(source(), make_fetcher({})).discover_page(None)


def test_the_content_signal_is_recorded_on_every_bridged_candidate():
    fetcher = build_fetcher()
    signal = parse_content_signal(fixture_text("doab_robots.txt"), "directory.doabooks.org")
    fetcher.content_signal_for = lambda url: signal

    result = DoabOapenBridgeAdapter(source(), fetcher).discover_page(None)

    for candidate in result.candidates:
        assert candidate.raw_metadata["content_signal"]["ai_train"] is False
        assert "no-ai-training" in candidate.raw_metadata["use_obligations"]


def test_a_fetcher_without_signal_support_does_not_break_discovery():
    """An older or simpler fetcher must not stop a harvest."""
    fetcher = build_fetcher()
    assert not hasattr(fetcher, "content_signal_for")

    result = DoabOapenBridgeAdapter(source(), fetcher).discover_page(None)
    assert result.candidates
    assert "content_signal" not in result.candidates[0].raw_metadata


def test_the_signal_serializes_for_the_database():
    payload = parse_content_signal(fixture_text("doab_robots.txt"), "d.org").to_dict()
    assert json.loads(json.dumps(payload))["obligations"] == ["no-ai-training", "use:reference"]


def test_an_undeclared_signal_has_no_obligations():
    assert ContentSignal(host="x.org").obligations() == ()
    assert not ContentSignal(host="x.org").forbids_training()


# ===================================================================
# Step 8: the broker and the curated allowlist
# ===================================================================


def test_europeana_declares_no_content_hosting():
    """
    **A broker describes; it does not host.** Europeana names objects
    held by hundreds of institutions and serves almost none of them, so
    the content role belongs to whichever provider `edmIsShownBy` points
    at -- and that host has to earn its own place on the allowlist.
    """
    from fieldhorizon.corpus.adapters.europeana import EUROPEANA_CAPABILITIES

    assert not EUROPEANA_CAPABILITIES.has(Capability.CONTENT_HOSTING)
    assert not EUROPEANA_CAPABILITIES.has(Capability.DISCOVERY), "not without the key"
    assert EUROPEANA_CAPABILITIES.has(Capability.DISCOVERY, credentials_available=True)
    assert EUROPEANA_CAPABILITIES.credential_env == "EUROPEANA_API_KEY"


def test_a_broker_pointing_off_the_allowlist_is_recorded_not_followed():
    """
    An aggregator followed blindly IS an allowlist bypass: Europeana can
    name any host on the internet. Phase I logged the refusal and moved
    on; the count now survives into the record, because "found 400,
    fetchable 12" is what tells an operator which institutions to
    allowlist next.
    """
    from fieldhorizon.corpus.adapters.europeana import parse_search_response

    payload = json.dumps({
        "success": True,
        "items": [{
            "id": "/123/abc",
            "title": ["A Book"],
            "rights": ["http://creativecommons.org/publicdomain/zero/1.0/"],
            "edmIsShownBy": ["https://not-allowlisted.example.org/object.pdf"],
            "edmIsShownAt": ["https://www.europeana.eu/item/123/abc"],
        }],
    }).encode()

    candidates, _ = parse_search_response(payload, "europeana", ["api.europeana.eu"])
    candidate = candidates[0]

    assert candidate.download_url == ""
    assert candidate.raw_metadata["provider_verified"] is False
    assert candidate.raw_metadata["unverified_provider_hosts"] == ["not-allowlisted.example.org"]
    assert "not allowlisted" in candidate.raw_metadata["acquisition_note"]
    assert candidate.rights.access_restricted


def test_a_broker_pointing_at_an_allowlisted_host_is_followed():
    from fieldhorizon.corpus.adapters.europeana import parse_search_response

    payload = json.dumps({
        "success": True,
        "items": [{
            "id": "/123/abc",
            "title": ["A Book"],
            "rights": ["http://creativecommons.org/publicdomain/zero/1.0/"],
            "edmIsShownBy": ["https://trusted.example.org/object.pdf"],
        }],
    }).encode()

    candidate = parse_search_response(
        payload, "europeana", ["api.europeana.eu", "trusted.example.org"]
    )[0][0]

    assert candidate.download_url == "https://trusted.example.org/object.pdf"
    assert candidate.raw_metadata["provider_verified"] is True
    assert candidate.raw_metadata["content_provider_host"] == "trusted.example.org"


def test_internet_archive_does_not_claim_to_supply_rights_evidence():
    """
    `licenseurl` is what an uploader typed. An uploader is not an
    authority on whether a work is in the public domain, and Phase I's
    single worst rights risk was treating that field as a determination.
    """
    from fieldhorizon.corpus.adapters.internet_archive import INTERNET_ARCHIVE_CAPABILITIES

    assert INTERNET_ARCHIVE_CAPABILITIES.has(Capability.DISCOVERY)
    assert INTERNET_ARCHIVE_CAPABILITIES.has(Capability.CONTENT_HOSTING)
    assert not INTERNET_ARCHIVE_CAPABILITIES.has(Capability.RIGHTS_EVIDENCE)


def test_the_curated_allowlist_is_read_under_either_spelling():
    """
    Phase I called it `allowed_collections`. Renaming it outright would
    silently empty the allowlist of any deployment still using the old
    key -- safe, since an empty allowlist quarantines everything, but
    indistinguishable from the harvester breaking.
    """
    from fieldhorizon.corpus.adapters.internet_archive import InternetArchiveAdapter

    def adapter(**options):
        return InternetArchiveAdapter(
            make_source("ia", "internet_archive", base_url="https://archive.org",
                        allowed_hosts=["archive.org"], options=options),
            make_fetcher({}),
        )

    assert adapter(trusted_collections=["library_of_congress"]).trusted_collections() == [
        "library_of_congress"
    ]
    assert adapter(allowed_collections=["nasa"]).trusted_collections() == ["nasa"]
    assert adapter().trusted_collections() == [], "empty by default: everything quarantines"
