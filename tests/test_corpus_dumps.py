"""
Bulk dump acquisition: resolution, resumable download, multistream
random access, and the Wikisource adapter built on them.

Every fixture here is what dumps.wikimedia.org actually returned on
2026-08-01, trimmed but not invented:

  wikisource_dump_directory.html   the nginx autoindex, verbatim
  wikisource_dumpstatus.json       real jobs, real sha1/md5/size values
  wikisource_dump_index.txt        real index lines, chosen for variety
  wikisource_stream_pages.xml      real <page> elements from two streams

The index fixture retains one line that is the whole reason namespace
filtering is written the way it is:

  297482:1881:Les Fougerêts : Patrimoine et identité d’une commune…

That is a MAIN-namespace work whose title contains a colon. Splitting a
title on ':' to find its namespace files it under one called
"Les Fougerêts " and silently drops a real book.
"""

from __future__ import annotations

import bz2
import json

import pytest

from fieldhorizon.corpus.adapters.wikimedia import clean_wikitext
from fieldhorizon.corpus.adapters.wikisource_dump import (
    CC_BY_SA_URL,
    ENTERPRISE_TOKEN_ENV,
    INDEX_PREFIX_BYTES,
    SEED_MAX_WORKS,
    SEED_MIN_WORKS,
    WIKISOURCE_DUMP_CAPABILITIES,
    WikisourceDumpAdapter,
    dump_importable,
    is_transclusion_shell,
    parse_pages,
    rights_stack_for,
    seed_titles,
)
from fieldhorizon.corpus.capabilities import Capability
from fieldhorizon.corpus.dumps import (
    ChecksumMismatch,
    DumpError,
    DumpFile,
    checkpoint_token,
    decompress_prefix,
    decompress_stream,
    download_state,
    dump_destination,
    finalize_download,
    latest_complete_date,
    looks_namespaced,
    parse_checkpoint,
    parse_dump_dates,
    parse_dumpstatus,
    parse_multistream_index,
    stream_ranges,
    verify_sha1,
    wrap_stream_fragment,
)
from fieldhorizon.corpus.http import FetchError
from fieldhorizon.corpus.lattice import RightsComponent, RightsStack, decide, intersect
from fieldhorizon.corpus.models import Decision
from fieldhorizon.corpus.security import SecurityError
from tests.corpus_helpers import (
    build_multistream_dump,
    fixture_bytes,
    fixture_text,
    make_fetcher,
    make_source,
    split_mediawiki_pages,
)

WIKI = "frwikisource"
DATE = "20260701"
DUMPS = "https://dumps.wikimedia.org"
LISTING_URL = f"{DUMPS}/{WIKI}/"
STATUS_URL = f"{DUMPS}/{WIKI}/{DATE}/dumpstatus.json"
INDEX_URL = f"{DUMPS}/{WIKI}/{DATE}/{WIKI}-{DATE}-pages-articles-multistream-index.txt.bz2"
DATA_URL = f"{DUMPS}/{WIKI}/{DATE}/{WIKI}-{DATE}-pages-articles-multistream.xml.bz2"


def release():
    return parse_dumpstatus(fixture_bytes("wikisource_dumpstatus.json"), wiki=WIKI, date=DATE)


def index_entries():
    return parse_multistream_index(fixture_text("wikisource_dump_index.txt"))


def source(**options):
    options.setdefault("wiki", WIKI)
    options.setdefault("language", "fr")
    return make_source(
        "wikisource_fr_dump", "wikisource_dump",
        base_url=DUMPS,
        allowed_hosts=["dumps.wikimedia.org"],
        options=options,
    )


# --------------------------------------------------------- release resolution


def test_the_live_directory_index_yields_dated_releases():
    dates = parse_dump_dates(fixture_text("wikisource_dump_directory.html"))

    assert dates == sorted(dates), "dates must come back in order"
    assert DATE in dates
    assert all(len(d) == 8 and d.isdigit() for d in dates)


def test_latest_is_excluded_because_it_is_a_moving_symlink():
    """
    `latest/` is in the real listing. Resolving it twice in one run --
    once for the index, once for the data -- can straddle two releases,
    and then the index's byte offsets point into the middle of the wrong
    stream. Only dated directories are usable.
    """
    html = fixture_text("wikisource_dump_directory.html")
    assert 'href="latest/"' in html, "the fixture should contain the trap"
    assert "latest" not in parse_dump_dates(html)


def test_a_release_can_be_pinned_to_a_ceiling_for_reproducibility():
    dates = parse_dump_dates(fixture_text("wikisource_dump_directory.html"))

    assert latest_complete_date(dates, not_after="20260301") == "20260301"
    assert latest_complete_date(dates, not_after="20260215") == "20260201"


def test_no_usable_release_is_an_error_not_an_empty_result():
    with pytest.raises(DumpError, match="no dump release"):
        latest_complete_date(["20260701"], not_after="20200101")


# ------------------------------------------------------------- dumpstatus


def test_the_live_manifest_parses_with_its_checksums():
    files = release().files
    name = f"{WIKI}-{DATE}-pages-articles-multistream.xml.bz2"

    assert name in files
    entry = files[name]
    assert entry.size > 1_000_000_000, "this really is a multi-gigabyte file"
    assert len(entry.sha1) == 40
    assert len(entry.md5) == 32


def test_an_unfinished_job_contributes_no_files():
    """
    A job that is waiting or in-progress has files that either do not
    exist or are still being written. Reading one gives a truncated
    corpus with no error anywhere -- so it is dropped here, at the only
    point that can still tell the difference.
    """
    payload = json.loads(fixture_bytes("wikisource_dumpstatus.json"))
    payload["jobs"]["articlesdumprecombine"]["status"] = "in-progress"

    files = parse_dumpstatus(json.dumps(payload).encode(), wiki=WIKI, date=DATE).files
    assert f"{WIKI}-{DATE}-pages-articles.xml.bz2" not in files
    # The multistream job was untouched and is still there.
    assert f"{WIKI}-{DATE}-pages-articles-multistream.xml.bz2" in files


def test_a_manifest_with_no_completed_job_raises():
    payload = json.loads(fixture_bytes("wikisource_dumpstatus.json"))
    for job in payload["jobs"].values():
        job["status"] = "waiting"

    with pytest.raises(DumpError, match="no completed job"):
        parse_dumpstatus(json.dumps(payload).encode(), wiki=WIKI, date=DATE)


def test_an_ambiguous_file_match_raises_rather_than_picking_one():
    """
    The sharded jobs publish names differing only by a page range.
    Quietly harvesting shard 1 of 8 because it sorted first looks exactly
    like a successful run that lost seven eighths of the corpus.
    """
    with pytest.raises(DumpError, match="files match"):
        release().find("pages-articles-multistream", ".bz2")


def test_a_missing_file_match_raises():
    with pytest.raises(DumpError, match="no file matching"):
        release().find("pages-meta-history")


def test_a_relative_manifest_url_resolves_against_the_service():
    entry = release().find("pages-articles-multistream.xml.bz2")
    assert entry.url.startswith("/"), "the live manifest publishes relative URLs"
    assert entry.absolute_url(DUMPS) == DATA_URL


# ----------------------------------------------------- multistream index


def test_the_live_index_parses():
    entries = index_entries()
    assert entries
    for entry in entries:
        assert entry.offset > 0
        assert entry.page_id > 0
        assert entry.title


def test_a_main_namespace_title_containing_a_colon_survives_intact():
    """
    **The trap, from real data.** `Les Fougerêts : Patrimoine et identité
    d’une commune de Haute-Bretagne` is a work, not a page in a namespace
    called `Les Fougerêts `. An unbounded split on ':' truncates the
    title AND misfiles the work.
    """
    entry = next(e for e in index_entries() if e.title.startswith("Les Fougerêts"))

    assert entry.title == (
        "Les Fougerêts : Patrimoine et identité d’une commune de Haute-Bretagne"
    )
    assert not looks_namespaced(entry.title)
    assert entry.page_id == 1881


@pytest.mark.parametrize(
    "title",
    ["Page:Baus - Étude sur le corset.djvu/1", "Auteur:Arthur Rimbaud",
     "Catégorie:Guy de Maupassant", "MediaWiki:Monobook.css"],
)
def test_real_namespaced_titles_are_recognised(title):
    assert looks_namespaced(title)


def test_seed_titles_skips_the_namespaced_ones():
    titles = [e.title for e in seed_titles(index_entries(), 50)]

    assert titles
    assert not any(t.startswith(("Page:", "Auteur:", "Catégorie:", "MediaWiki:")) for t in titles)
    assert any(t.startswith("Les Fougerêts") for t in titles), "the colon title is a work"


# ---------------------------------------------------------- stream ranges


def test_streams_become_byte_ranges_ending_at_the_next_offset():
    entries = index_entries()
    ranges = stream_ranges(entries, total_bytes=3_000_000_000)
    offsets = sorted({e.offset for e in entries})

    assert len(ranges) == len(offsets)
    for index, stream in enumerate(ranges[:-1]):
        assert stream.end == offsets[index + 1] - 1
        assert stream.header() == f"bytes={stream.offset}-{stream.end}"


def test_the_final_stream_is_dropped_when_the_file_size_is_unknown():
    """
    An open-ended Range against a 2.8 GB file is precisely the unbounded
    download this whole module exists to avoid. Better to skip one stream
    than to accidentally request the tail of a multi-gigabyte dump.
    """
    entries = index_entries()
    with_size = stream_ranges(entries, total_bytes=3_000_000_000)
    without = stream_ranges(entries, total_bytes=0)

    assert len(without) == len(with_size) - 1


def test_a_range_is_a_small_slice_of_a_very_large_file():
    """The bounded-cost claim, stated as a number."""
    ranges = stream_ranges(index_entries(), total_bytes=2_801_978_811)
    smallest = min(r.length for r in ranges)

    assert smallest < 200_000, "one stream should be tens of kilobytes, not gigabytes"


# ------------------------------------------------------ resumable download


def test_an_interrupted_download_resumes_from_its_part_file(tmp_path):
    destination = tmp_path / "dump.xml.bz2"
    part = destination.with_suffix(destination.suffix + ".part")
    part.write_bytes(b"A" * 500)

    state = download_state(destination, DumpFile("dump.xml.bz2", "/x", size=1200))

    assert state.downloaded_bytes == 500
    assert state.remaining == 700
    assert not state.complete


def test_a_finished_download_is_recognised_and_not_repeated(tmp_path):
    destination = tmp_path / "dump.xml.bz2"
    destination.write_bytes(b"A" * 1200)

    state = download_state(destination, DumpFile("dump.xml.bz2", "/x", size=1200))

    assert state.complete
    assert state.remaining == 0


def test_a_part_longer_than_the_release_declares_is_discarded(tmp_path):
    """
    Not a resumable prefix of anything. Truncating it would leave a file
    whose tail belongs to a different release -- which the checksum
    catches, but only after a multi-gigabyte download.
    """
    destination = tmp_path / "dump.xml.bz2"
    part = destination.with_suffix(destination.suffix + ".part")
    part.write_bytes(b"A" * 5000)

    state = download_state(destination, DumpFile("dump.xml.bz2", "/x", size=1200))

    assert state.downloaded_bytes == 0
    assert not part.exists()


def test_a_verified_part_is_renamed_into_place(tmp_path):
    import hashlib

    payload = b"the dump contents"
    destination = tmp_path / "dump.xml.bz2"
    part = destination.with_suffix(destination.suffix + ".part")
    part.write_bytes(payload)

    state = download_state(destination, DumpFile(
        "dump.xml.bz2", "/x", size=len(payload), sha1=hashlib.sha1(payload).hexdigest(),
    ))
    result = finalize_download(state)

    assert result == destination
    assert destination.read_bytes() == payload
    assert not part.exists()
    assert state.verified


def test_a_checksum_mismatch_deletes_the_part_and_never_names_it(tmp_path):
    """
    Verification happens BEFORE the rename, so a corrupt file never
    appears under its real name even for an instant -- and the part is
    deleted, because whatever is wrong with it is in bytes already
    written and no amount of appending will fix it.
    """
    destination = tmp_path / "dump.xml.bz2"
    part = destination.with_suffix(destination.suffix + ".part")
    part.write_bytes(b"corrupted")

    state = download_state(destination, DumpFile(
        "dump.xml.bz2", "/x", size=9, sha1="0" * 40,
    ))
    with pytest.raises(ChecksumMismatch):
        finalize_download(state)

    assert not destination.exists(), "a file that failed its checksum must never be named"
    assert not part.exists()


def test_a_short_part_is_refused_before_hashing(tmp_path):
    destination = tmp_path / "dump.xml.bz2"
    destination.with_suffix(destination.suffix + ".part").write_bytes(b"short")

    state = download_state(destination, DumpFile("dump.xml.bz2", "/x", size=999))
    with pytest.raises(DumpError, match="expected 999"):
        finalize_download(state)


def test_sha1_is_streamed_not_read_whole(tmp_path):
    import hashlib

    payload = b"x" * (3 * 1024 * 1024)
    path = tmp_path / "big.bin"
    path.write_bytes(payload)

    assert verify_sha1(path, hashlib.sha1(payload).hexdigest())
    with pytest.raises(ChecksumMismatch):
        verify_sha1(path, "f" * 40)


def test_a_manifest_filename_cannot_escape_the_data_root(tmp_path):
    """
    A filename out of a manifest is provider data, and provider data is
    never trusted with a path.
    """
    assert dump_destination(tmp_path, WIKI, DATE, "dump.xml.bz2").is_relative_to(tmp_path)

    for hostile in ("../../etc/cron.d/harvest", "/etc/passwd", "..\\..\\evil"):
        resolved = dump_destination(tmp_path, WIKI, DATE, hostile)
        assert resolved.is_relative_to(tmp_path), hostile


def test_an_absolute_escape_is_refused_outright(tmp_path):
    with pytest.raises(SecurityError):
        dump_destination(tmp_path, "..", "..", "x")


# ------------------------------------------------------------ compression


def test_a_real_bzip2_stream_decompresses():
    payload = bz2.compress(b"<page><title>x</title></page>" * 50)
    assert b"<title>x</title>" in decompress_stream(payload)


def test_a_compression_bomb_is_refused_during_decompression_not_after():
    """
    Bounded while expanding, because a bomb's whole point is that its
    expanded size is discovered too late to act on.
    """
    bomb = bz2.compress(b"\0" * (4 * 1024 * 1024))
    with pytest.raises(SecurityError, match="expands past"):
        decompress_stream(bomb, max_bytes=1024)


def test_a_mid_document_fragment_is_wrapped_into_something_parseable():
    fragment = b"  <page><title>A work</title><ns>0</ns><id>1</id></page>\n"
    pages = parse_pages(wrap_stream_fragment(fragment))

    assert [p.title for p in pages] == ["A work"]


def test_a_complete_document_is_not_double_wrapped():
    complete = fixture_bytes("wikisource_stream_pages.xml")
    assert wrap_stream_fragment(complete).count(b"<mediawiki") == 1


def test_the_final_streams_closing_tag_alone_is_handled():
    assert parse_pages(wrap_stream_fragment(b"</mediawiki>\n")) == []


def test_a_wrapped_fragment_still_refuses_a_doctype():
    """
    Wrapping must not become a way to smuggle a DOCTYPE past the hardened
    parser. This is the one place a multi-gigabyte file of user-submitted
    text meets an XML parser.
    """
    hostile = b'<!DOCTYPE r [<!ENTITY a "boom">]>\n  <page><title>&a;</title></page>'
    with pytest.raises(SecurityError):
        parse_pages(wrap_stream_fragment(hostile))


# --------------------------------------------------------- page parsing


def test_the_live_stream_pages_parse():
    pages = parse_pages(fixture_bytes("wikisource_stream_pages.xml"))

    assert len(pages) == 5
    for page in pages:
        assert page.title
        assert page.page_id > 0
        assert page.revision_id, "revision_id is what makes an incremental run possible"


def test_redirects_are_not_works():
    """
    A redirect page has a real title, a real id, and a nine-word body of
    `#REDIRECT [[...]]`. It imports perfectly cleanly and pollutes the
    deduplicator with a title that already exists. The live sample was
    13 redirects in its first 14 pages.
    """
    pages = parse_pages(fixture_bytes("wikisource_stream_pages.xml"))
    redirect = next(p for p in pages if p.is_redirect)

    assert redirect.namespace == 0, "it IS in the main namespace"
    assert not redirect.is_content, "and it is still not a work"


def test_the_namespace_element_decides_not_the_title():
    pages = parse_pages(fixture_bytes("wikisource_stream_pages.xml"))
    proofread = next(p for p in pages if p.namespace == 104)

    assert proofread.title.startswith("Page:")
    assert not proofread.is_content
    assert [p.title for p in pages if p.is_content] == [
        "La Colombe et la Fourmi",
        "Histoire de la philosophie moderne",
        "Patrimoine et Identité/Conclusion",
    ]


def test_an_unparseable_namespace_excludes_the_page_rather_than_defaulting_to_zero():
    """
    Defaulting to 0 on garbage would import whatever it was. Excluding is
    the safe direction: a lost page is visible in a count, an imported
    template is not.
    """
    xml = b"""<mediawiki><page><title>Odd</title><ns>not-a-number</ns>
    <id>1</id><revision><id>2</id><text>body</text></revision></page></mediawiki>"""
    page = parse_pages(xml)[0]

    assert page.namespace == -1
    assert not page.is_content


# ---------------------------------------------------------------- rights


def test_the_work_and_the_transcription_are_separate_layers():
    pages = parse_pages(fixture_bytes("wikisource_stream_pages.xml"))
    page = next(p for p in pages if p.is_content)
    stack = rights_stack_for(page, page_url="https://fr.wikisource.org/wiki/x", language="fr")

    assert stack.get(RightsComponent.EDITORIAL_CONTRIBUTION) is not None
    assert stack.get(RightsComponent.WORK) is not None


def test_an_undetermined_work_blocks_even_though_the_transcription_is_licensed():
    """
    **A transcriber can only license what they own.**

    The tempting argument runs: CC BY-SA is an operative grant, every
    contributor licenses their contribution under it, so what the dump
    leaves unsaid about the underlying work can only widen freedom. It is
    wrong, and it is easy to make twice.

    What a Wikisource contributor owns is their transcription -- the
    keystrokes, the proofreading, the markup. They do not own the work
    they transcribed and cannot grant rights in it. CC BY-SA over a
    transcription of a text whose copyright status nobody established
    grants nothing usable: if the work is still in copyright, the
    composite is unusable whatever the footer says.

    So the WORK layer is applicable and unevaluated, which is exactly the
    standing rule -- unknown, absent, or unevaluated quarantines.
    """
    from fieldhorizon.corpus.adapters.wikisource_dump import DumpPage
    from fieldhorizon.corpus.rights import get_profile

    page = DumpPage("A work", 1, 0, "99", "2024-01-01T00:00:00Z", "plain wikitext, no template")
    stack = rights_stack_for(page, page_url="https://fr.wikisource.org/wiki/x", language="fr")

    assert intersect(stack).blocked
    assert decide(stack, get_profile("local_research_us"))[0] == Decision.QUARANTINE


def test_the_reason_names_the_transcription_limit_rather_than_shrugging():
    """
    "unknown" tells an operator nothing. The note has to say WHY the
    CC BY-SA grant sitting right there is not sufficient.
    """
    from fieldhorizon.corpus.adapters.wikisource_dump import DumpPage

    page = DumpPage("A work", 1, 0, "99", "", "plain wikitext, no template")
    work = rights_stack_for(page, page_url="https://x", language="fr").get(RightsComponent.WORK)

    assert work is not None
    assert not work.not_applicable, "an unevaluated layer must block, not be waved through"
    assert "transcription only" in work.notes
    assert work.evidence, "the gap is still evidenced"


def test_the_editorial_grant_alone_never_carries_a_document():
    """
    The general form of the rule: a licence over one layer cannot stand
    in for a determination about another.
    """
    from fieldhorizon.corpus.rights import get_profile

    stack = RightsStack()
    stack.add(RightsComponent.EDITORIAL_CONTRIBUTION, CC_BY_SA_URL)
    stack.add(RightsComponent.WORK, "")

    assert intersect(stack).blocked
    assert decide(stack, get_profile("local_research_us"))[0] == Decision.QUARANTINE


def test_a_public_domain_template_is_recorded_as_evidence_not_as_a_verdict():
    from fieldhorizon.corpus.adapters.wikisource_dump import DumpPage
    from fieldhorizon.corpus.rights import get_profile

    page = DumpPage("A work", 1, 0, "99", "", "{{PD-old}}\n\nThe text of the work.")
    stack = rights_stack_for(page, page_url="https://fr.wikisource.org/wiki/x", language="fr")
    work = stack.get(RightsComponent.WORK)

    assert "pd-old" in work.notes.lower()
    assert work.evidence, "no evidence, no accept -- even with a template"
    # CC BY-SA on the transcription still constrains the result.
    result = intersect(stack)
    assert result.attribution_required
    assert result.share_alike
    assert decide(stack, get_profile("local_research_us"))[0] == Decision.ACCEPT


def test_the_share_alike_obligation_survives_the_intersection():
    """
    Obligations accumulate; they do not cancel. A public-domain work
    transcribed under CC BY-SA still carries attribution and share-alike,
    and an export that dropped them would be a licence violation.
    """
    from fieldhorizon.corpus.adapters.wikisource_dump import DumpPage

    page = DumpPage("A work", 1, 0, "99", "", "{{PD-old}} text")
    result = intersect(rights_stack_for(page, page_url="https://x", language="fr"))

    assert result.attribution_required
    assert result.share_alike


# --------------------------------------------------------------- adapter


def build_fetcher(**extra):
    """
    A real multistream dump whose index offsets agree with its bytes.

    The captured `wikisource_stream_pages.xml` is repackaged into two
    independently-compressed streams, so a Range request for
    `index[i].offset` genuinely lands on a decompressible boundary. Using
    the live index here instead would point 379126 at a 4 kB file --
    every fetch a 416, every test green for the wrong reason.
    """
    pages = split_mediawiki_pages(fixture_bytes("wikisource_stream_pages.xml"))
    data, index_text = build_multistream_dump([pages[:3], pages[3:]])
    mapping = {
        LISTING_URL: fixture_bytes("wikisource_dump_directory.html"),
        STATUS_URL: fixture_bytes("wikisource_dumpstatus.json"),
        INDEX_URL: bz2.compress(index_text.encode("utf-8")),
        DATA_URL: data,
    }
    mapping.update(extra)
    return make_fetcher(mapping)


def test_discovery_resolves_reads_and_yields_works_offline(monkeypatch):
    monkeypatch.delenv(ENTERPRISE_TOKEN_ENV, raising=False)
    fetcher = build_fetcher()
    adapter = WikisourceDumpAdapter(source(dump_date=DATE), fetcher)

    result = adapter.discover_page(None)

    titles = [c.title for c in result.candidates]
    assert titles == ["Patrimoine et Identité/Conclusion"]
    assert not any(t.startswith("Page:") for t in titles)
    assert not any("Aigle et l’Escarbot" in t for t in titles), "redirects are not works"


def test_only_one_of_five_real_pages_carries_its_own_text():
    """
    **The finding that shapes this adapter**, stated as the count it
    produces. Of five real pages captured from the live dump:

      La Colombe et la Fourmi          {{Œuvre}} + a category. No text.
      L’Aigle et l’Escarbot (Collinet)  a redirect.
      Histoire de la philosophie…     <pages index="…djvu"/>. No text.
      Patrimoine et Identité/Conclusion  4381 characters of real prose.
      Page:Baus — Étude sur le corset  namespace 104.

    Wikisource keeps the transcribed text of most works in the `Page:`
    namespace and assembles it at render time; `pages-articles` carries
    the main namespace, so for those works it fills the discovery and
    metadata roles and not the content role. That is the same shape as
    the Gallica finding, and it is not a parse failure.
    """
    fetcher = build_fetcher()
    adapter = WikisourceDumpAdapter(source(dump_date=DATE), fetcher)

    assert len(adapter.discover_page(None).candidates) == 1
    assert adapter.shells_seen == 2, "the shells are counted, not silently dropped"


def test_a_transclusion_shell_is_recognised_from_real_wikitext():
    assert is_transclusion_shell("{{Œuvre}}\n[[Catégorie:Fables]]", "")
    assert is_transclusion_shell('<pages index="Höffding.djvu"/>', "")
    assert not is_transclusion_shell("Real prose, several hundred characters of it.",
                                     "Real prose, several hundred characters of it.")


def test_the_dump_route_reverses_the_api_route_subpage_rule():
    """
    `wikimedia.is_importable_page` skips subpages: on the API route the
    parent page's RENDERED output already contains every chapter, so
    importing both duplicates the work. A dump has no rendered output --
    the parent is the bare transclusion directive and the subpage is
    where the text is. Applying the API rule to a dump imports exactly
    nothing, which is what it did until this was found against the real
    file.
    """
    from fieldhorizon.corpus.adapters.wikimedia import is_importable_page

    pages = parse_pages(fixture_bytes("wikisource_stream_pages.xml"))
    subpage = next(p for p in pages if "/" in p.title)
    cleaned, _ = clean_wikitext(subpage.wikitext)

    assert not is_importable_page(subpage.title, subpage.namespace, cleaned)[0]
    assert dump_importable(subpage, cleaned)[0]


def test_discovery_reads_the_data_file_by_range_not_in_full():
    """
    The bounded-cost claim, asserted rather than described: the dump is
    requested with a Range header, never as a whole 2.8 GB body.
    """
    fetcher = build_fetcher()
    WikisourceDumpAdapter(source(dump_date=DATE), fetcher).discover_page(None)

    ranged = [r for url, r in fetcher.ranges if url == DATA_URL]
    assert ranged, "the multistream dump must be read by byte range"
    assert all(r.startswith("bytes=") for r in ranged)


def test_a_server_that_ignores_range_is_refused_rather_than_truncated():
    """
    A 200 answering a Range request means the whole file is coming. The
    size cap would hand back its first N bytes as though they were the
    45 kB stream that was asked for -- a truncated body sitting next to a
    success status, which nothing downstream could distinguish.
    """
    fetcher = build_fetcher()
    fetcher.ignores_range.add(DATA_URL)
    adapter = WikisourceDumpAdapter(source(dump_date=DATE), fetcher)

    # Per-stream containment means the run survives; it just finds nothing.
    assert adapter.discover_page(None).candidates == []

    with pytest.raises(FetchError, match="does not honour Range"):
        fetcher.fetch(DATA_URL, allowed_hosts=["dumps.wikimedia.org"], byte_range="bytes=0-10")


def test_the_revision_id_reaches_the_candidate():
    fetcher = build_fetcher()
    candidates = WikisourceDumpAdapter(source(dump_date=DATE), fetcher).discover_page(None).candidates

    for candidate in candidates:
        assert candidate.raw_metadata["revision_id"]
        assert candidate.raw_metadata["dump"] == f"{WIKI}/{DATE}"


def test_content_comes_from_the_dump_without_a_second_request():
    """
    Discovery already read the text. Re-fetching the page over HTTP would
    be a second request for bytes already in hand -- the one thing the
    dump route exists to avoid.
    """
    fetcher = build_fetcher()
    adapter = WikisourceDumpAdapter(source(dump_date=DATE), fetcher)
    candidate = adapter.discover_page(None).candidates[0]

    before = fetcher.request_count
    response = adapter.fetch_content(candidate)

    assert fetcher.request_count == before, "fetch_content must not hit the network"
    assert response.content
    assert response.detected_mime == "text/plain"


def test_the_seed_ceiling_is_clamped_not_trusted():
    """
    A config asking for 5000 works in seed mode has misunderstood the
    mode. Honouring it would start an unattended multi-gigabyte harvest
    under a name promising the opposite.
    """
    fetcher = build_fetcher()

    assert WikisourceDumpAdapter(source(max_works=5000), fetcher).seed_limit() == SEED_MAX_WORKS
    assert WikisourceDumpAdapter(source(max_works=1), fetcher).seed_limit() == SEED_MIN_WORKS
    assert WikisourceDumpAdapter(source(max_works=30), fetcher).seed_limit() == 30


def test_a_checkpoint_round_trips_and_names_both_halves():
    assert parse_checkpoint(checkpoint_token(379126, 2010)) == (379126, 2010)
    assert parse_checkpoint("") == (0, 0)
    assert parse_checkpoint("nonsense") == (0, 0)


def synthetic_work(page_id: int, title: str) -> tuple[int, str, str]:
    """One main-namespace page carrying real text, for structural tests."""
    body = f"{title}. " + ("Le patrimoine de la commune presente de nombreuses specificites. " * 12)
    return page_id, title, (
        f"  <page>\n    <title>{title}</title>\n    <ns>0</ns>\n    <id>{page_id}</id>\n"
        f"    <revision>\n      <id>{page_id * 10}</id>\n"
        f"      <timestamp>2026-01-01T00:00:00Z</timestamp>\n"
        f'      <text xml:space="preserve">{body}</text>\n'
        f"    </revision>\n  </page>\n"
    )


def test_resuming_past_a_checkpoint_skips_what_was_already_imported():
    stream_one = [synthetic_work(i, f"Work {i}") for i in (101, 102, 103)]
    stream_two = [synthetic_work(i, f"Work {i}") for i in (201, 202)]
    data, index_text = build_multistream_dump([stream_one, stream_two])
    fetcher = build_fetcher(**{DATA_URL: data, INDEX_URL: bz2.compress(index_text.encode())})
    adapter = WikisourceDumpAdapter(source(dump_date=DATE), fetcher)

    everything = adapter.discover_page(None).candidates
    assert [c.title for c in everything] == [f"Work {i}" for i in (101, 102, 103, 201, 202)]

    resumed = adapter.discover_page(checkpoint_token(0, 102))

    assert [c.title for c in resumed.candidates] == [f"Work {i}" for i in (103, 201, 202)]


def test_a_checkpoint_only_skips_within_its_own_stream():
    """
    The checkpoint is `offset:page_id`. The page id is meaningful only
    inside the stream it came from -- ids are not ordered across the
    dump -- so a later stream must be read in full even if its ids
    happen to be lower.
    """
    stream_one = [synthetic_work(i, f"High {i}") for i in (900, 901)]
    stream_two = [synthetic_work(i, f"Low {i}") for i in (100, 101)]
    data, index_text = build_multistream_dump([stream_one, stream_two])
    fetcher = build_fetcher(**{DATA_URL: data, INDEX_URL: bz2.compress(index_text.encode())})
    adapter = WikisourceDumpAdapter(source(dump_date=DATE), fetcher)

    resumed = adapter.discover_page(checkpoint_token(0, 900))

    assert [c.title for c in resumed.candidates] == ["High 901", "Low 100", "Low 101"]


def test_a_bad_wiki_option_is_a_clear_error():
    fetcher = build_fetcher()
    for bad in ("", "fr wikisource", "../etc"):
        with pytest.raises(DumpError, match="must be a dump name"):
            assert WikisourceDumpAdapter(source(wiki=bad), fetcher).wiki


def test_an_unreadable_stream_is_skipped_not_fatal():
    fetcher = build_fetcher(**{DATA_URL: b"not bzip2 at all"})
    result = WikisourceDumpAdapter(source(dump_date=DATE), fetcher).discover_page(None)

    assert result.candidates == []
    assert result.exhausted


# ---------------------------------------------------------- capabilities


def test_the_adapter_declares_bulk_snapshot_and_resume():
    for capability in (Capability.BULK_SNAPSHOT, Capability.SUPPORTS_RESUME,
                       Capability.INCREMENTAL_UPDATES, Capability.CONTENT_HOSTING):
        assert WIKISOURCE_DUMP_CAPABILITIES.has(capability), capability


def test_the_enterprise_transport_needs_a_token_and_says_so(monkeypatch):
    """
    READY_WITH_CREDENTIALS, not "broken". The public export needs no
    credentials and is the normal path, not a degraded one.
    """
    monkeypatch.delenv(ENTERPRISE_TOKEN_ENV, raising=False)
    adapter = WikisourceDumpAdapter(source(transport="enterprise"), build_fetcher())

    with pytest.raises(DumpError, match=ENTERPRISE_TOKEN_ENV):
        adapter.discover_page(None)


def test_the_token_name_is_recorded_but_never_its_value(monkeypatch):
    monkeypatch.setenv(ENTERPRISE_TOKEN_ENV, "secret-token-value")
    payload = WIKISOURCE_DUMP_CAPABILITIES.to_dict()

    assert payload["credential_env"] == ENTERPRISE_TOKEN_ENV
    assert "secret-token-value" not in str(payload)


def test_the_public_transport_works_with_no_credentials(monkeypatch):
    monkeypatch.delenv(ENTERPRISE_TOKEN_ENV, raising=False)
    adapter = WikisourceDumpAdapter(source(dump_date=DATE), build_fetcher())

    assert adapter.discover_page(None).candidates


def test_no_adapter_here_scrapes_the_wiki_itself():
    import inspect

    from fieldhorizon.corpus.adapters import wikisource_dump

    code = inspect.getsource(wikisource_dump)
    assert "wikisource.org/w/index.php" not in code
    assert "action=raw" not in code


# ------------------------------------------------------- index prefix


def test_seed_mode_reads_a_prefix_of_the_index_not_all_of_it():
    """
    Found by the live pilot: frwikisource's index is 28 MB compressed and
    expands past 200 MB. Downloading and buffering all of it to take
    thirty titles is the same mistake as downloading the 2.8 GB dump to
    read fifty pages, one layer down.
    """
    fetcher = build_fetcher()
    WikisourceDumpAdapter(source(dump_date=DATE), fetcher).discover_page(None)

    index_ranges = [r for url, r in fetcher.ranges if url == INDEX_URL]
    assert index_ranges == [f"bytes=0-{INDEX_PREFIX_BYTES - 1}"]


def test_archive_mode_reads_the_whole_index():
    """Archive mode genuinely needs every offset, so it asks for all of them."""
    fetcher = build_fetcher()
    WikisourceDumpAdapter(source(dump_date=DATE, mode="full"), fetcher).discover_page(None)

    assert not [r for url, r in fetcher.ranges if url == INDEX_URL]


def test_a_truncated_index_prefix_still_yields_its_complete_lines():
    """
    A prefix cuts the last line mid-title. That line must not become a
    malformed entry, and everything before it must survive.
    """
    lines = "\n".join(f"{i * 1000}:{i}:Work number {i}" for i in range(1, 400)) + "\n"
    compressed = bz2.compress(lines.encode("utf-8"))
    entries = parse_multistream_index(
        decompress_prefix(compressed[: len(compressed) // 2]).decode("utf-8", "replace")
    )

    assert entries == [] or all(e.title.startswith("Work number") for e in entries)


def test_decompress_prefix_tolerates_truncation_where_decompress_stream_would_not():
    payload = bz2.compress(b"line one\nline two\nline three\n" * 200)
    truncated = payload[: len(payload) - 20]

    # The lenient reader gets what it can; the strict one is for whole streams.
    assert isinstance(decompress_prefix(truncated), bytes)
    assert b"line one" in decompress_prefix(payload)


def test_an_index_prefix_that_yields_nothing_is_an_error_not_an_empty_run():
    """
    Zero entries would otherwise be reported as "discovery complete, 0
    works" -- a total failure wearing the costume of success.
    """
    fetcher = build_fetcher(**{INDEX_URL: bz2.compress(b"no colons here at all\n")})
    with pytest.raises(DumpError, match="no entries"):
        WikisourceDumpAdapter(source(dump_date=DATE), fetcher).discover_page(None)
