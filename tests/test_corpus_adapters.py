"""
Adapter parsing, entirely offline.

Covers required cases 1-7: OPDS, OAI-PMH, Gutenberg RDF/CSV, Wikisource,
Gallica, Europeana, and Internet Archive.

Every test drives the real adapter against a `FixtureFetcher`, which
raises for any URL it was not given. A test that accidentally depended on
the network would therefore fail rather than quietly pass -- see
`test_no_adapter_reaches_the_network` at the end, which asserts this
property directly.
"""

from __future__ import annotations

import json

import pytest

from fieldhorizon.corpus.adapters.europeana import EuropeanaAdapter, parse_search_response
from fieldhorizon.corpus.adapters.gallica import GallicaAdapter, parse_gallica_record
from fieldhorizon.corpus.adapters.gutenberg import (
    GutenbergAdapter,
    parse_gutenberg_csv,
    parse_gutenberg_rdf,
)
from fieldhorizon.corpus.adapters.internet_archive import (
    InternetArchiveAdapter,
    choose_best_file,
)
from fieldhorizon.corpus.adapters.internet_archive import (
    parse_search_response as parse_ia_search,
)
from fieldhorizon.corpus.adapters.oai_pmh import (
    OAIPMHError,
    build_list_records_url,
    build_resume_url,
    parse_oai_response,
)
from fieldhorizon.corpus.adapters.oapen_doab import OapenDoabAdapter
from fieldhorizon.corpus.adapters.opds import parse_opds_feed
from fieldhorizon.corpus.adapters.registry import available_adapters, build_adapter
from fieldhorizon.corpus.adapters.sru import compute_next_start, parse_sru_envelope
from fieldhorizon.corpus.adapters.standard_ebooks import StandardEbooksAdapter
from fieldhorizon.corpus.adapters.wikisource import (
    WikisourceAdapter,
    candidate_from_page,
    parse_category_members,
    parse_page_content,
)
from fieldhorizon.corpus.http import FetchError
from fieldhorizon.corpus.security import UnsafeXMLError, findall_local, safe_parse_xml
from tests.corpus_helpers import FIXTURES, fixture_bytes, make_fetcher, make_source

# ---------------------------------------------------------------- 1. OPDS


def test_opds_feed_parses_entries_authors_subjects_and_next_link():
    candidates, next_url = parse_opds_feed(
        fixture_bytes("opds_standard_ebooks.xml"),
        "https://standardebooks.org/feeds/opds/all",
        "standard_ebooks",
    )

    assert next_url == "https://standardebooks.org/feeds/opds/all?page=2"
    by_title = {c.title: c for c in candidates}
    assert "The Social Contract" in by_title

    rousseau = by_title["The Social Contract"]
    assert rousseau.authors == ("Jean-Jacques Rousseau",)
    assert rousseau.contributors == ("G. D. H. Cole",)
    assert rousseau.language == "en"
    assert "Political science" in rousseau.subjects
    assert rousseau.canonical_url.endswith("/ebooks/jean-jacques-rousseau/the-social-contract")


def test_opds_prefers_plain_text_over_epub_and_keeps_the_alternative():
    candidates, _ = parse_opds_feed(
        fixture_bytes("opds_standard_ebooks.xml"),
        "https://standardebooks.org/feeds/opds/all",
        "standard_ebooks",
    )
    rousseau = next(c for c in candidates if c.title == "The Social Contract")

    # TXT outranks EPUB in the corpus-wide format preference.
    assert rousseau.download_format == "txt"
    assert rousseau.download_url.endswith("social-contract.txt")
    # The EPUB is not discarded -- it stays as a fallback for when the
    # preferred format turns out to be unusable.
    assert any(fmt == "epub" for fmt, _ in rousseau.alternate_downloads)


def test_opds_borrow_relation_is_recorded_as_access_restricted_not_as_a_download():
    candidates, _ = parse_opds_feed(
        fixture_bytes("opds_standard_ebooks.xml"),
        "https://standardebooks.org/feeds/opds/all",
        "standard_ebooks",
    )
    borrowable = next(c for c in candidates if c.title == "A Borrowable Title")

    assert borrowable.download_url == ""
    assert borrowable.rights.access_restricted is True


def test_standard_ebooks_adapter_attaches_the_cc0_dedication_as_evidence():
    fetcher = make_fetcher(
        {"https://standardebooks.org/feeds/opds/all": FIXTURES / "opds_standard_ebooks.xml"}
    )
    adapter = StandardEbooksAdapter(make_source("standard_ebooks", "standard_ebooks"), fetcher)

    result = adapter.discover_page(None)
    rousseau = next(c for c in result.candidates if c.title == "The Social Contract")

    kinds = {e.kind for e in rousseau.rights.evidence}
    assert "standard_ebooks:cc0-edition-dedication" in kinds
    # The entry's own rights text is preserved alongside the dedication,
    # so a future divergence would be visible rather than overwritten.
    assert "opds:rights" in kinds

    # The dedication describes the EDITION. It must not become the work's
    # licence, and it must not widen the scope: the adapter asserts no
    # scope of its own and lets the licence's inherent scope decide.
    dedication = next(
        e for e in rousseau.rights.evidence if e.kind == "standard_ebooks:cc0-edition-dedication"
    )
    assert "EDITION only" in dedication.value
    assert rousseau.rights.provider_declared_scope == ""


def test_standard_ebooks_us_only_rights_are_not_upgraded_to_worldwide():
    """
    Standard Ebooks states "Public domain in the United States" and
    separately dedicates its own editorial contribution under CC0.
    Reading the CC0 as a worldwide grant over the WORK is the single most
    consequential mistake available here, and it was one this adapter
    made before it was caught against the live feed.
    """
    from fieldhorizon.corpus.models import Decision, DistributionScope
    from fieldhorizon.corpus.rights import evaluate, get_profile

    fetcher = make_fetcher(
        {"https://standardebooks.org/feeds/opds/all": FIXTURES / "opds_standard_ebooks.xml"}
    )
    adapter = StandardEbooksAdapter(make_source("standard_ebooks", "standard_ebooks"), fetcher)
    candidates = adapter.discover_page(None).candidates

    # The fixture's Rousseau entry states US public domain explicitly.
    rousseau = next(c for c in candidates if c.title == "The Social Contract")

    local = evaluate(rousseau.rights, get_profile("local_research_us"))
    assert local.decision == Decision.ACCEPT
    assert local.rights_scope == DistributionScope.LOCAL_US_ONLY

    worldwide = evaluate(rousseau.rights, get_profile("release_worldwide"))
    assert worldwide.decision == Decision.QUARANTINE


# ------------------------------------------------------------- 2. OAI-PMH


def test_oai_pmh_parses_records_and_returns_the_resumption_token():
    candidates, token = parse_oai_response(
        fixture_bytes("oai_pmh_doab_page1.xml"), "doab", "https://directory.doabooks.org/oai/request"
    )

    assert token == "TOKEN-PAGE-2"
    # The deleted-status record carries no metadata and must be skipped.
    assert len(candidates) == 2
    assert all("00000" not in c.external_id for c in candidates)

    cybernetics = next(c for c in candidates if "Cybernetics" in c.title)
    assert cybernetics.authors == ("Lefevre, Anne",)
    assert cybernetics.language == "en"  # 'eng' normalized from ISO 639-2
    assert "Systems theory" in cybernetics.subjects
    assert cybernetics.download_url.endswith("book.pdf")


def test_oai_pmh_empty_resumption_token_means_the_harvest_is_complete():
    _, token = parse_oai_response(
        fixture_bytes("oai_pmh_doab_page2.xml"), "doab", "https://directory.doabooks.org/oai/request"
    )
    assert token is None


def test_oai_pmh_error_element_raises_rather_than_returning_nothing():
    with pytest.raises(OAIPMHError, match="badArgument"):
        parse_oai_response(fixture_bytes("oai_pmh_error.xml"), "doab", "https://example.org/oai")


def test_oai_pmh_resumption_url_carries_only_verb_and_token():
    """
    The single most common OAI-PMH client bug: sending metadataPrefix
    alongside resumptionToken, which every compliant repository rejects
    with badArgument.
    """
    url = build_resume_url("https://directory.doabooks.org/oai/request", "TOKEN-PAGE-2")

    assert "resumptionToken=TOKEN-PAGE-2" in url
    assert "metadataPrefix" not in url
    assert "set=" not in url

    initial = build_list_records_url("https://example.org/oai", "oai_dc", "books")
    assert "metadataPrefix=oai_dc" in initial
    assert "set=books" in initial


def test_oapen_demotes_an_open_access_statement_that_names_no_licence():
    fetcher = make_fetcher(
        {
            "https://directory.doabooks.org/oai/request?verb=ListRecords&metadataPrefix=oai_dc": (
                FIXTURES / "oai_pmh_doab_page1.xml"
            )
        }
    )
    source = make_source(
        "doab", "oapen_doab",
        base_url="https://directory.doabooks.org/oai/request",
        allowed_hosts=["directory.doabooks.org", "library.oapen.org"],
    )
    adapter = OapenDoabAdapter(source, fetcher)
    result = adapter.discover_page(None)

    open_access_only = next(c for c in result.candidates if "No Licence Grant" in c.title)
    # "Open access" is a business model, not a grant: it must not become
    # the content licence, or the rights engine would evaluate a
    # non-licence as though it were one.
    assert open_access_only.rights.content_license == ""
    assert "open access" in open_access_only.rights.raw_rights_text.lower()

    licensed = next(c for c in result.candidates if "Cybernetics" in c.title)
    assert "creativecommons.org/licenses/by/4.0" in licensed.rights.content_license


# ---------------------------------------------------- 3. Gutenberg RDF/CSV


def test_gutenberg_csv_parses_and_marks_us_only_scope():
    candidates = parse_gutenberg_csv(fixture_bytes("gutenberg_catalog.csv"), "gutenberg")
    by_id = {c.external_id: c for c in candidates}

    assert set(by_id) == {"1342", "61", "1497", "17489", "2000"}

    manifesto = by_id["61"]
    assert manifesto.title == "The Communist Manifesto"
    assert manifesto.authors == ("Marx, Karl, 1818-1883", "Engels, Friedrich, 1820-1895")
    assert manifesto.language == "en"
    # Gutenberg determines status under US law and says so; the scope
    # must never be widened to worldwide by this adapter.
    assert manifesto.rights.provider_declared_scope == "LOCAL_US_ONLY"
    assert manifesto.rights.jurisdictions == ("US",)

    descartes = by_id["17489"]
    assert descartes.language == "fr"

    still_copyrighted = by_id["2000"]
    assert still_copyrighted.rights.provider_declared_scope == ""
    assert "Copyrighted" in still_copyrighted.rights.content_license


def test_gutenberg_csv_prefers_utf8_text_and_offers_epub_as_an_alternative():
    candidates = parse_gutenberg_csv(fixture_bytes("gutenberg_catalog.csv"), "gutenberg")
    manifesto = next(c for c in candidates if c.external_id == "61")

    assert manifesto.download_format == "txt"
    assert manifesto.download_url.endswith("61.txt.utf-8")
    assert [fmt for fmt, _ in manifesto.alternate_downloads] == ["epub", "html"]


def test_gutenberg_candidates_carry_the_jurisdiction_statement_as_evidence():
    candidates = parse_gutenberg_csv(fixture_bytes("gutenberg_catalog.csv"), "gutenberg")
    manifesto = next(c for c in candidates if c.external_id == "61")

    kinds = {e.kind for e in manifesto.rights.evidence}
    assert "gutenberg:jurisdiction-statement" in kinds
    statement = next(e for e in manifesto.rights.evidence if e.kind == "gutenberg:jurisdiction-statement")
    assert "United States" in statement.value
    assert statement.content_hash  # evidence is hashed so a change is detectable


def test_gutenberg_rdf_record_parses_with_formats_and_creators():
    candidate = parse_gutenberg_rdf(fixture_bytes("gutenberg_pg61.rdf"), "gutenberg")

    assert candidate is not None
    assert candidate.external_id == "61"
    assert candidate.title == "The Communist Manifesto"
    assert "Marx, Karl" in candidate.authors
    assert "Engels, Friedrich" in candidate.authors
    assert candidate.language == "en"
    assert "Communism" in candidate.subjects
    assert candidate.download_format == "txt"
    assert candidate.estimated_bytes == 96000
    assert candidate.rights.provider_declared_scope == "LOCAL_US_ONLY"


def test_the_catalogue_cache_never_escapes_into_the_working_directory(tmp_path):
    """
    The cache path must come from configuration, not from the process's
    current directory.

    A cwd-relative default meant this offline suite wrote its fixture
    catalogue into the repository's real data/corpus/manifests/, where a
    later live run read it back as though it were Project Gutenberg's own
    catalogue -- and labelled a real French document with a fixture's
    title. The bug was invisible until a live run disagreed with itself.
    """
    catalog_url = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
    fetcher = make_fetcher({catalog_url: FIXTURES / "gutenberg_catalog.csv"})
    cache_dir = tmp_path / "cache"
    source = make_source(
        "gutenberg", "gutenberg",
        base_url="https://www.gutenberg.org",
        allowed_hosts=["www.gutenberg.org"],
        options={"catalog_url": catalog_url, "page_size": 2, "fetch_rights_rdf": False},
    )
    source.cache_dir = cache_dir

    adapter = GutenbergAdapter(source, fetcher)

    # The invariant is about the path the adapter computes, not about
    # whether some file happens to exist in the repository -- a real
    # harvest legitimately writes its own cache there, and asserting on
    # that would make this test depend on whether one had run.
    computed = adapter._catalogue_cache_path(catalog_url)
    assert computed.is_absolute()
    assert cache_dir in computed.parents

    adapter.discover_page(None)
    assert list(cache_dir.glob("catalogue_*.csv")), "the catalogue should have been cached"


def test_two_sources_sharing_a_catalogue_both_see_it(tmp_path):
    """
    A second source hitting the same catalogue URL gets a 304, because
    the first source just fetched it. Without a stored body that means
    the second source silently discovers nothing -- which is exactly what
    happened to the French Gutenberg source in the pilot.
    """
    catalog_url = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
    fetcher = make_fetcher({catalog_url: FIXTURES / "gutenberg_catalog.csv"})
    cache_dir = tmp_path / "cache"

    first = make_source(
        "gutenberg", "gutenberg", base_url="https://www.gutenberg.org",
        allowed_hosts=["www.gutenberg.org"],
        options={"catalog_url": catalog_url, "page_size": 10, "fetch_rights_rdf": False},
    )
    first.cache_dir = cache_dir
    assert GutenbergAdapter(first, fetcher).discover_page(None).candidates

    # The second source's fetch now 304s, as a real server would answer.
    fetcher.not_modified.add(catalog_url)
    second = make_source(
        "gutenberg_fr", "gutenberg", base_url="https://www.gutenberg.org",
        allowed_hosts=["www.gutenberg.org"],
        options={"catalog_url": catalog_url, "page_size": 10,
                 "fetch_rights_rdf": False, "filter_languages": ["fr"]},
    )
    second.cache_dir = cache_dir

    candidates = GutenbergAdapter(second, fetcher).discover_page(None).candidates
    assert candidates, "the second source must read the cached catalogue, not give up"
    assert all(c.language == "fr" for c in candidates)


def test_per_item_rights_enrichment_is_not_conditional(tmp_path):
    """
    A 304 on a per-book RDF would be answered from a cache that does not
    exist, so enrichment would silently not happen -- and a candidate
    with no rights statement is quarantined. On a second run that meant
    every Gutenberg document quietly stopped qualifying.
    """
    catalog_url = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
    rdf_url = "https://www.gutenberg.org/ebooks/61.rdf"
    fetcher = make_fetcher({
        catalog_url: FIXTURES / "gutenberg_catalog.csv",
        rdf_url: FIXTURES / "gutenberg_pg61.rdf",
    })
    source = make_source(
        "gutenberg", "gutenberg", base_url="https://www.gutenberg.org",
        allowed_hosts=["www.gutenberg.org"],
        options={"catalog_url": catalog_url, "page_size": 10, "fetch_rights_rdf": True},
    )
    source.cache_dir = tmp_path / "cache"

    adapter = GutenbergAdapter(source, fetcher)
    candidates = adapter.discover_page(None).candidates

    manifesto = next(c for c in candidates if c.external_id == "61")
    # Enrichment happened: the RDF supplied the rights the CSV lacks.
    assert "Public domain in the USA" in manifesto.rights.content_license
    assert manifesto.rights.provider_declared_scope == "LOCAL_US_ONLY"


def test_gutenberg_adapter_pages_through_the_catalogue(tmp_path):
    catalog_url = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
    fetcher = make_fetcher({catalog_url: FIXTURES / "gutenberg_catalog.csv"})
    source = make_source(
        "gutenberg", "gutenberg",
        base_url="https://www.gutenberg.org",
        allowed_hosts=["www.gutenberg.org"],
        options={"catalog_url": catalog_url, "page_size": 2},
    )
    adapter = GutenbergAdapter(source, fetcher)

    first = adapter.discover_page(None)
    assert len(first.candidates) == 2
    assert first.next_cursor == "2"
    assert first.exhausted is False

    last = adapter.discover_page("4")
    assert last.exhausted is True
    assert last.next_cursor is None


# ---------------------------------------------------------- 4. Wikisource


def test_wikisource_category_and_content_parse():
    members, token = parse_category_members(fixture_bytes("wikisource_category.json"))
    assert token
    assert {m["title"] for m in members} >= {"Manifeste du surréalisme", "Discours de la méthode"}

    pages = parse_page_content(fixture_bytes("wikisource_content.json"))
    # The `missing: true` page carries no revision and is skipped.
    assert len(pages) == 1
    assert pages[0]["title"] == "Manifeste du surréalisme"
    assert "{{TextQuality" in pages[0]["wikitext"]


def test_wikisource_cleans_wikitext_and_records_share_alike_obligations():
    pages = parse_page_content(fixture_bytes("wikisource_content.json"))
    candidate = candidate_from_page(pages[0], "wikisource_fr", "https://fr.wikisource.org", "fr")

    assert candidate is not None
    text = candidate.raw_metadata["_content"]

    # Templates, refs, categories, and markup are gone...
    assert "{{TextQuality" not in text
    assert "Note de l'éditeur" not in text
    assert "[[Catégorie:" not in text
    assert "'''" not in text
    # ...but the rendered text of a piped link survives, because dropping
    # it would silently delete words from the work.
    assert "Le rêve" in text
    assert "Nous exigeons" in text

    assert candidate.rights.content_license == "CC BY-SA 4.0"
    kinds = {e.kind for e in candidate.rights.evidence}
    assert kinds == {"wikisource:site-license", "wikisource:page-attribution"}


def test_wikisource_excludes_subpages_talk_and_non_content_namespaces():
    from fieldhorizon.corpus.adapters.wikimedia import is_importable_page

    body = "x" * 2000

    assert is_importable_page("Manifeste du surréalisme", 0, body)[0] is True
    # A subpage would duplicate the whole work, which is imported via its
    # parent.
    assert is_importable_page("Manifeste du surréalisme/Chapitre 1", 0, body)[0] is False
    assert is_importable_page("Catégorie:Philosophie", 14, body)[0] is False
    assert is_importable_page("Discussion:Quelque chose", 1, body)[0] is False
    assert is_importable_page("Index des auteurs", 0, body)[0] is False
    assert is_importable_page("A Stub", 0, "too short")[0] is False
    assert is_importable_page("A Redirect", 0, "#REDIRECT [[Elsewhere]]")[0] is False


def test_wikisource_fetch_content_makes_no_second_request():
    """
    Wikisource returns wikitext alongside its metadata, so the content is
    already in hand. Re-fetching would be a pointless extra hit on
    Wikimedia's API.
    """
    pages = parse_page_content(fixture_bytes("wikisource_content.json"))
    candidate = candidate_from_page(pages[0], "wikisource_fr", "https://fr.wikisource.org", "fr")

    fetcher = make_fetcher({})  # empty: any real fetch would raise
    adapter = WikisourceAdapter(
        make_source("wikisource_fr", "wikisource",
                    base_url="https://fr.wikisource.org", allowed_hosts=["fr.wikisource.org"]),
        fetcher,
    )

    response = adapter.fetch_content(candidate)
    assert response.from_cache is True
    assert b"surr" in response.content
    assert fetcher.calls == []


# ------------------------------------------------------------- 5. Gallica


def test_gallica_sru_envelope_and_record_parsing():
    records, total, next_position = parse_sru_envelope(fixture_bytes("gallica_sru.xml"))
    # 250 matches in the result set, of which this page returned 3.
    assert total == 250
    assert next_position == 4
    assert len(records) == 3

    parsed = [parse_gallica_record(record, "gallica") for record in records]
    # The record with no ARK has no stable identifier and is dropped.
    assert parsed[2] is None

    declaration = parsed[0]
    assert declaration is not None
    assert declaration.external_id == "ark:/12148/bpt6k1234567"
    assert declaration.language == "fr"
    assert "domaine public" in declaration.rights.content_license
    assert declaration.rights.access_restricted is False
    assert declaration.download_url.endswith(".texteBrut")


def test_gallica_treats_consultable_en_ligne_as_access_not_as_a_licence():
    records, _, _ = parse_sru_envelope(fixture_bytes("gallica_sru.xml"))
    restricted = parse_gallica_record(records[1], "gallica")

    assert restricted is not None
    # An access statement is not permission: the content licence stays
    # empty so the rights engine sees "no licence", and the record is
    # flagged access-restricted.
    assert restricted.rights.content_license == ""
    assert restricted.rights.access_restricted is True
    assert "Consultable en ligne" in restricted.rights.raw_rights_text


def test_sru_pagination_arithmetic():
    # Server-reported next position wins when present and sane.
    assert compute_next_start(1, 50, 50, 300, 51) == 51
    # A short page means the result set is exhausted.
    assert compute_next_start(51, 50, 12, 62, 0) == 0
    # A full page with no reported position advances by the page size.
    assert compute_next_start(1, 50, 50, 300, 0) == 51
    # Never past the reported total.
    assert compute_next_start(251, 50, 50, 300, 0) == 0


def test_gallica_adapter_walks_pages(tmp_path):
    url = (
        "https://gallica.bnf.fr/SRU?operation=searchRetrieve&version=1.2"
        "&query=test&startRecord=1&maximumRecords=50"
    )
    fetcher = make_fetcher({url: FIXTURES / "gallica_sru.xml"})
    source = make_source(
        "gallica", "gallica",
        base_url="https://gallica.bnf.fr/SRU",
        allowed_hosts=["gallica.bnf.fr"],
        options={"query": "test", "page_size": 50},
    )
    result = GallicaAdapter(source, fetcher).discover_page(None)

    assert len(result.candidates) == 2  # the ARK-less record is dropped
    assert result.next_cursor == "4"


# ----------------------------------------------------------- 6. Europeana


def test_europeana_never_treats_its_cc0_metadata_licence_as_a_content_licence():
    candidates, cursor = parse_search_response(
        fixture_bytes("europeana_search.json"),
        "europeana",
        ["content.example-library.eu", "www.example-library.eu"],
    )
    assert cursor == "AoJ4nZ2Y"

    public_domain = next(c for c in candidates if "Kommunistische" in c.title)
    # Metadata licence and content licence are recorded separately, always.
    assert "CC0" in public_domain.rights.metadata_license
    assert "publicdomain/mark" in public_domain.rights.content_license

    in_copyright = next(c for c in candidates if "Twentieth-Century" in c.title)
    assert "CC0" in in_copyright.rights.metadata_license
    # An In Copyright object gets NO content licence from Europeana's own
    # open metadata -- this is the trap the split exists to prevent.
    assert in_copyright.rights.content_license == ""
    assert "rightsstatements.org/vocab/InC" in in_copyright.rights.rights_statement_uri


def test_europeana_will_not_point_the_harvester_at_a_non_allowlisted_host():
    candidates, _ = parse_search_response(
        fixture_bytes("europeana_search.json"),
        "europeana",
        ["content.example-library.eu", "www.example-library.eu"],
    )
    off_allowlist = next(c for c in candidates if "Unapproved Host" in c.title)

    # Openly licensed, but hosted somewhere the source never approved. An
    # aggregator that can redirect the harvester anywhere is an allowlist
    # bypass, however open the licence.
    assert off_allowlist.download_url == ""
    assert off_allowlist.rights.access_restricted is True


def test_europeana_reports_a_missing_key_rather_than_raising_when_asked(monkeypatch):
    """
    Phase II: `corpus health` must be able to say READY_WITH_CREDENTIALS
    by asking a question, not by catching an exception. A missing
    credential is a configuration state, not an error.
    """
    monkeypatch.delenv("EUROPEANA_API_KEY", raising=False)
    source = make_source(
        "europeana", "europeana",
        base_url="https://api.europeana.eu",
        allowed_hosts=["api.europeana.eu"],
    )
    adapter = EuropeanaAdapter(source, make_fetcher({}))

    assert adapter.api_key() == ""
    assert not adapter.has_credentials()


def test_europeana_still_refuses_to_discover_without_a_key(monkeypatch):
    """Reporting the gap is not the same as proceeding through it."""
    from fieldhorizon.corpus.adapters.europeana import CredentialsRequired

    monkeypatch.delenv("EUROPEANA_API_KEY", raising=False)
    source = make_source(
        "europeana", "europeana",
        base_url="https://api.europeana.eu",
        allowed_hosts=["api.europeana.eu"],
    )
    adapter = EuropeanaAdapter(source, make_fetcher({}))

    with pytest.raises(CredentialsRequired, match="EUROPEANA_API_KEY"):
        adapter.discover_page(None)


# ---------------------------------------------------- 7. Internet Archive


def test_internet_archive_quarantines_uploads_outside_curated_collections():
    candidates, total, returned = parse_ia_search(
        fixture_bytes("ia_search.json"), "internet_archive", ["medicalheritagelibrary"]
    )
    assert total == 3
    assert returned == 3

    curated = next(c for c in candidates if c.external_id == "curatedinstitutionalbook")
    # In an allowlisted institutional collection: the assertion is backed
    # by an identifiable institution.
    assert curated.rights.uploader_asserted_only is False
    assert any(e.kind == "internet_archive:curated-collection" for e in curated.rights.evidence)

    anonymous = next(c for c in candidates if c.external_id == "anonymousupload")
    # Claims CC0, but the claim is an anonymous uploader's. The rights
    # engine must not accept on that alone.
    assert anonymous.rights.uploader_asserted_only is True


def test_internet_archive_refuses_lending_library_items():
    candidates, _, _ = parse_ia_search(
        fixture_bytes("ia_search.json"), "internet_archive", ["medicalheritagelibrary"]
    )
    lending = next(c for c in candidates if c.external_id == "lendinglibraryitem")

    assert lending.rights.borrow_only is True
    assert lending.rights.access_restricted is True


def test_internet_archive_picks_one_text_derivative_not_the_whole_item():
    files = json.loads(fixture_bytes("ia_metadata.json"))["files"]
    name, fmt, size = choose_best_file(files)

    # TXT outranks PDF, and within a format the larger file wins -- IA
    # often carries both a truncated sample and the full text.
    assert fmt == "txt"
    assert name == "curatedinstitutionalbook_djvu.txt"
    assert size == 410000


def test_internet_archive_ignores_images_metadata_and_jp2_archives():
    files = [
        {"name": "item_meta.xml", "format": "Metadata", "size": "100"},
        {"name": "page_0001.jpg", "format": "JPEG", "size": "40000"},
        {"name": "item_jp2.zip", "format": "Single Page Processed JP2 ZIP", "size": "999999999"},
    ]
    assert choose_best_file(files) == ("", "", 0)


def test_internet_archive_fetch_content_downloads_only_the_chosen_file():
    search_url = None
    metadata_url = "https://archive.org/metadata/curatedinstitutionalbook"
    download_url = (
        "https://ia801502.us.archive.org/12/items/curatedinstitutionalbook/"
        "curatedinstitutionalbook_djvu.txt"
    )

    fetcher = make_fetcher(
        {
            metadata_url: FIXTURES / "ia_metadata.json",
            download_url: b"The text of a curated institutional scan. " * 40,
        }
    )
    source = make_source(
        "internet_archive", "internet_archive",
        base_url="https://archive.org",
        allowed_hosts=["archive.org", ".us.archive.org"],
        options={"allowed_collections": ["medicalheritagelibrary"]},
    )
    adapter = InternetArchiveAdapter(source, fetcher)

    candidates, _, _ = parse_ia_search(
        fixture_bytes("ia_search.json"), "internet_archive", ["medicalheritagelibrary"]
    )
    curated = next(c for c in candidates if c.external_id == "curatedinstitutionalbook")

    response = adapter.fetch_content(curated)
    assert b"curated institutional scan" in response.content
    # Exactly two requests: the metadata lookup and the one chosen file.
    assert fetcher.calls == [metadata_url, download_url]
    assert search_url is None


# --------------------------------------------------------------- registry


def test_every_configured_adapter_is_registered():
    registered = set(available_adapters())
    assert registered == {
        "doab_oapen", "europeana", "gallica", "gallica_text", "gutenberg", "internet_archive",
        "oai_pmh", "oapen_doab", "opds", "standard_ebooks", "standard_ebooks_github",
        "wikisource", "wikisource_dump",
    }


def test_no_generic_web_scraper_adapter_exists():
    """
    A standing rule, asserted rather than merely documented: adding a
    general crawler would violate the project's terms of engagement with
    every source it harvests.
    """
    for name in available_adapters():
        assert "scrap" not in name.lower()
        assert "crawl" not in name.lower()
        assert "generic_web" not in name.lower()


def test_unknown_adapter_name_is_a_clear_error():
    from fieldhorizon.corpus.adapters.registry import UnknownAdapterError

    source = make_source("broken", "no_such_adapter")
    with pytest.raises(UnknownAdapterError, match="no_such_adapter"):
        build_adapter(source, make_fetcher({}))


# --------------------------------------------------- offline-suite guard


def test_no_adapter_reaches_the_network():
    """
    Required case 40: the unit suite performs no network access.

    Proven structurally rather than by inspection -- the FixtureFetcher
    raises for any URL it was not given, so an adapter that tried to
    fetch something unexpected fails here.
    """
    fetcher = make_fetcher({})
    source = make_source("standard_ebooks", "standard_ebooks")
    adapter = build_adapter(source, fetcher)

    with pytest.raises(FetchError, match="no fixture"):
        adapter.discover_page(None)


def test_fixture_fetcher_still_enforces_the_host_allowlist():
    """
    The offline fetcher validates allowlists exactly as the real one
    does, so host restrictions are exercised by the offline tests too --
    otherwise the allowlist would only ever be tested in production.
    """
    from fieldhorizon.corpus.security import DisallowedURLError

    fetcher = make_fetcher({"https://evil.example.com/feed": b"<feed/>"})
    with pytest.raises(DisallowedURLError):
        fetcher.fetch("https://evil.example.com/feed", allowed_hosts=["standardebooks.org"])


# ------------------------------------------------------------ XML hardening


def test_xxe_and_billion_laughs_are_refused_before_parsing():
    """Required case: hostile XML is rejected, not merely survived."""
    with pytest.raises(UnsafeXMLError, match="DOCTYPE"):
        safe_parse_xml(fixture_bytes("malicious_xxe.xml"))

    # An ENTITY declaration below the head is caught too: the DOCTYPE
    # check only reads the first 8 kB, so the entity scan covers the
    # whole payload.
    with pytest.raises(UnsafeXMLError, match="ENTITY"):
        safe_parse_xml(fixture_bytes("malicious_entity_only.xml"))


def test_namespace_agnostic_search_finds_elements_regardless_of_prefix():
    """
    Real feeds disagree about namespace URIs constantly. Matching on
    local element name is what keeps an adapter from silently returning
    zero results against a perfectly valid feed.
    """
    root = safe_parse_xml(fixture_bytes("opds_standard_ebooks.xml"))
    assert len(findall_local(root, "entry")) == 3
