"""
Wikisource at institutional scale, from the published XML dumps.

The API adapter (`wikisource.py`) is right for targeted, incremental
work. It is the wrong tool for a catalogue: reading frwikisource through
`action=query` means hundreds of thousands of requests against an
institution that has already packaged the entire wiki into one file and
asked people to use it instead.

**The bounded-cost design.** `frwikisource-pages-articles-multistream`
is 2.8 GB, and the brief's `dump-seed` mode wants 20-50 works. A
multistream dump is a concatenation of independently-decompressible
bzip2 streams of 100 pages each, with a companion index mapping page
titles to byte offsets. Fifty works therefore cost the index plus a
handful of HTTP Range requests -- about 1-2 MB read out of 2.8 GB.
Verified live on 2026-08-01: a Range request for one stream decompresses
standalone into exactly 100 `<page>` elements.

The full-dump path exists too, for the archive profile, with a resumable
`.part` file and SHA-1 verification against the published manifest. It is
not what `dump-seed` uses.

**Two transports.**

  `xml_export`   dumps.wikimedia.org. Public, no credentials, the
                 default. Everything above.
  `enterprise`   Wikimedia Enterprise. Requires a token; declared as a
                 credentialled capability so `corpus health` reports
                 READY_WITH_CREDENTIALS rather than pretending it works.

There is no third transport that scrapes fr.wikisource.org, and adding
one would violate this project's terms of engagement with every source
it harvests.

**Rights are two layers, again.** Wikisource's own contribution -- the
transcription, proofreading, and markup -- is CC BY-SA 4.0, which
carries real attribution and share-alike obligations. The underlying
work is usually public domain, and separately so. Phase I recorded one
licence and got CC BY-SA for a public-domain text, which understates the
work's freedom and overstates the obligations on it. Both layers are now
stated and intersected.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ..capabilities import Capability, SourceCapabilities
from ..dumps import (
    DumpError,
    DumpRelease,
    IndexEntry,
    StreamRange,
    checkpoint_token,
    decompress_prefix,
    decompress_stream,
    latest_complete_date,
    parse_checkpoint,
    parse_dump_dates,
    parse_dumpstatus,
    parse_multistream_index,
    stream_ranges,
    wrap_stream_fragment,
)
from ..language import normalize_language_code
from ..lattice import RightsComponent, RightsStack
from ..models import Candidate, Evidence, RightsSignal
from ..rights import evidence_from_field
from ..security import child_local, findall_local, safe_parse_xml
from .base import DiscoveryResult, SourceAdapter, register_adapter
from .wikimedia import clean_wikitext

logger = logging.getLogger(__name__)

DUMPS_BASE = "https://dumps.wikimedia.org"
ENTERPRISE_TOKEN_ENV = "WIKIMEDIA_ENTERPRISE_TOKEN"

CC_BY_SA_URL = "https://creativecommons.org/licenses/by-sa/4.0/"

#: MediaWiki's main namespace. The authority on a page's namespace is the
#: `<ns>` element in the dump, never the title: the live French index
#: contains `Les Fougerêts : Patrimoine et identité d’une commune de
#: Haute-Bretagne`, a main-namespace work whose title contains a colon.
MAIN_NAMESPACE = 0

#: `dump-seed` is bounded by the brief to 20-50 works. The ceiling is
#: enforced, not advisory: the whole point of seed mode is that it cannot
#: turn into an unattended multi-gigabyte harvest.
SEED_MIN_WORKS = 20
SEED_MAX_WORKS = 50

#: How much of the multistream index seed mode reads. The live
#: frwikisource index is 28 MB compressed and expands past 200 MB; its
#: first 300 kB held 29,126 entries across 292 streams. 512 kB is a
#: comfortable margin over what seed mode can possibly use.
INDEX_PREFIX_BYTES = 512 * 1024

#: The ceiling for reading an index WHOLE, in archive mode. Well above
#: the largest real index and still far below anything that threatens
#: the process.
MAX_INDEX_BYTES = 512 * 1024 * 1024

WIKISOURCE_DUMP_CAPABILITIES = SourceCapabilities(
    provider_id="wikisource_dump",
    capabilities=frozenset(
        {
            Capability.DISCOVERY,
            Capability.METADATA,
            Capability.RIGHTS_EVIDENCE,
            Capability.CONTENT_HOSTING,
            Capability.RENDERED_TEXT,
            Capability.BULK_SNAPSHOT,
            Capability.INCREMENTAL_UPDATES,
            Capability.SUPPORTS_RESUME,
        }
    ),
    credentialled_capabilities=frozenset({Capability.BULK_SNAPSHOT}),
    credential_env=ENTERPRISE_TOKEN_ENV,
    rights_trust=0.85,
    content_hosts=("dumps.wikimedia.org",),
    notes=(
        "Public XML content-file exports, used as published, with the service's own "
        "SHA-1 manifest. The Wikimedia Enterprise transport additionally requires "
        f"${ENTERPRISE_TOKEN_ENV}; without it the public export is used, which is not "
        "a degraded mode but the normal one."
    ),
)


# --------------------------------------------------------------------------
# Page parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DumpPage:
    """One `<page>` element from an XML export."""

    title: str
    page_id: int
    namespace: int
    revision_id: str
    timestamp: str
    wikitext: str
    is_redirect: bool = False

    @property
    def is_content(self) -> bool:
        """
        A work, as opposed to a redirect or a non-content page.

        `<ns>` is the authority. `<redirect>` matters because a redirect
        page has a real title, a real id, and a body of `#REDIRECT
        [[...]]` -- it would import cleanly as a nine-word document and
        pollute the deduplicator with a title that already exists. The
        live sample was 13 redirects in the first 14 pages.
        """
        return self.namespace == MAIN_NAMESPACE and not self.is_redirect


def parse_pages(xml_bytes: bytes) -> list[DumpPage]:
    """
    Parse a `<mediawiki>` document -- or a wrapped stream fragment -- into
    pages.

    `safe_parse_xml` refuses DOCTYPE and entity declarations, which
    matters more here than anywhere else in the harvester: this is the
    one place where a multi-gigabyte file of user-submitted text is
    handed to an XML parser.
    """
    root = safe_parse_xml(xml_bytes)
    pages: list[DumpPage] = []

    for element in findall_local(root, "page"):
        title_el = child_local(element, "title")
        ns_el = child_local(element, "ns")
        id_el = child_local(element, "id")
        title = (title_el.text or "").strip() if title_el is not None else ""
        if not title:
            continue

        revision = child_local(element, "revision")
        revision_id = ""
        timestamp = ""
        wikitext = ""
        if revision is not None:
            revision_id_el = child_local(revision, "id")
            timestamp_el = child_local(revision, "timestamp")
            text_el = child_local(revision, "text")
            revision_id = (revision_id_el.text or "").strip() if revision_id_el is not None else ""
            timestamp = (timestamp_el.text or "").strip() if timestamp_el is not None else ""
            wikitext = (text_el.text or "") if text_el is not None else ""

        try:
            namespace = int((ns_el.text or "0").strip()) if ns_el is not None else 0
        except ValueError:
            namespace = -1  # Unparseable: not the main namespace, so excluded.

        try:
            page_id = int((id_el.text or "0").strip()) if id_el is not None else 0
        except ValueError:
            page_id = 0

        pages.append(
            DumpPage(
                title=title,
                page_id=page_id,
                namespace=namespace,
                revision_id=revision_id,
                timestamp=timestamp,
                wikitext=wikitext,
                is_redirect=child_local(element, "redirect") is not None,
            )
        )
    return pages


# --------------------------------------------------------------------------
# Rights
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# What a dump can and cannot give
# --------------------------------------------------------------------------

#: Markers of a page whose body is assembled at render time from the
#: `Page:` namespace rather than stored in the page itself.
_TRANSCLUSION_MARKERS = (
    "<pages index=",       # the ProofreadPage extension's assembly directive
    "<pages\n",
    "{{:",                 # whole-page transclusion of a subpage
)

#: Templates that constitute an entire page in the dump: a header saying
#: "this is a work" and nothing else.
_SHELL_ONLY_TEMPLATES = ("{{œuvre}}", "{{oeuvre}}", "{{textquality")


def is_transclusion_shell(wikitext: str, cleaned: str) -> bool:
    """
    Whether a page's text lives somewhere the dump does not carry it.

    **This is the Wikisource equivalent of the Gallica finding, and it is
    not a bug.** `pages-articles` contains the main namespace. Wikisource
    stores the actual transcribed text of most works in the `Page:`
    namespace, one page per scanned image, and the main-namespace entry
    is a directive that assembles them at render time. Two real examples
    from the captured dump:

        La Colombe et la Fourmi        {{Œuvre}} + a category. Nothing else.
        Histoire de la philosophie…    <pages index="Höffding - …djvu"/>

    Both are genuine, complete, correct Wikisource pages. Neither
    contains a word of its own text. A harvester that treats them as
    empty documents records a parse failure; what actually happened is
    that this dump fills the discovery and metadata roles for these works
    and does not fill the content role -- which is CONTENT_UNAVAILABLE,
    a state, not an error.
    """
    lowered = (wikitext or "").lower()
    if cleaned.strip():
        return False
    if any(marker in lowered for marker in _TRANSCLUSION_MARKERS):
        return True
    return any(template in lowered for template in _SHELL_ONLY_TEMPLATES)


def dump_importable(page: DumpPage, cleaned: str) -> tuple[bool, str]:
    """
    Whether a dump page carries an importable work, and why not if not.

    Deliberately NOT `wikimedia.is_importable_page`. That function skips
    subpages, because on the API route the parent page's *rendered*
    output already contains every chapter and importing both would
    duplicate the work. A dump has no rendered output: the parent is the
    bare transclusion directive and the subpages are where the text
    actually is. Applying the API rule to a dump imports exactly nothing
    -- which is what it did, until this was found against the real file.
    """
    if page.is_redirect:
        return False, "redirect"
    if page.namespace != MAIN_NAMESPACE:
        return False, f"namespace {page.namespace} is not the main namespace"

    body = cleaned.strip()
    if body.lower().startswith(("#redirect", "#redirection")):
        return False, "redirect"
    if is_transclusion_shell(page.wikitext, cleaned):
        return False, "transclusion shell: the text is in the Page: namespace, not in this dump"
    if len(body) < MIN_DUMP_CONTENT_CHARS:
        return False, f"insufficient content ({len(body)} chars)"
    return True, ""


#: Below this, a main-namespace page is a stub or a navigation aid rather
#: than a work. Lower than the API route's threshold on purpose: a dump
#: subpage is one chapter, not a whole book.
MIN_DUMP_CONTENT_CHARS = 400


def rights_stack_for(page: DumpPage, *, page_url: str, language: str) -> RightsStack:
    """
    Two layers: the work, and Wikisource's transcription of it.

    Phase I recorded CC BY-SA alone, which is what the site's footer
    says. That understates a public-domain work's freedom and overstates
    the obligations attached to it -- and, in the other direction, it
    would have been the only thing recorded even for a work that is NOT
    public domain, where CC BY-SA is doing all the load-bearing work.

    **An undetermined work layer BLOCKS.** A live seed of thirty works
    came back 30/30 quarantined, and that is the correct answer.

    The tempting argument against it goes: CC BY-SA is an operative
    grant, every contributor licenses their contribution under it, so
    what the dump leaves unsaid about the underlying work can only widen
    freedom. That argument is wrong, and the reason is worth stating
    because it is easy to make twice.

    **A transcriber can only license what they own.** What a Wikisource
    contributor owns is their transcription -- the keystrokes, the
    proofreading, the markup. They do not own the work they transcribed
    and cannot grant anyone rights in it. So CC BY-SA over a transcription
    of a text whose copyright status nobody has established grants
    nothing usable: if the underlying work is still in copyright, the
    composite is unusable no matter what the transcriber wrote in the
    footer.

    The WORK layer is therefore applicable to the ingested text and
    unevaluated, which is precisely the case the standing rule names:
    an unknown, absent, or unevaluated licence quarantines. Lattice rule
    1 -- a blocking component blocks all -- exists for exactly this.

    A public-domain template on the page is a claim by the community,
    recorded as evidence and checked against the licence vocabulary. It
    is the only thing that fills this layer from a dump, and where it is
    absent the document waits for a human or for `corpus audit-rights`.
    """
    evidence: tuple[Evidence, ...] = (
        evidence_from_field(
            "wikisource:site-licence",
            "Text on Wikisource is available under the Creative Commons "
            "Attribution-ShareAlike 4.0 licence. Attribution is to the page and its "
            "contributor history.",
            page_url,
        ),
        evidence_from_field("wikisource:revision", page.revision_id, page_url),
    )

    stack = RightsStack()
    stack.add(
        RightsComponent.EDITORIAL_CONTRIBUTION,
        CC_BY_SA_URL,
        rights_statement_uri=CC_BY_SA_URL,
        evidence=evidence,
        notes="Wikisource's transcription, proofreading, and markup",
    )

    work_licence, work_note = _work_licence_from_wikitext(page.wikitext)
    stack.add(
        RightsComponent.WORK,
        work_licence,
        evidence=evidence,
        # Stated even when empty, which normalizes to UNKNOWN and blocks.
        # Omitting the layer would let the transcription's CC BY-SA stand
        # in for a determination about the work that nobody has made.
        notes=work_note or (
            f"no per-work rights statement in the {language} dump; the CC BY-SA "
            "grant covers the transcription only and cannot establish the "
            "status of the work transcribed"
        ),
    )
    return stack


#: Public-domain templates the French and English Wikisources use. A
#: template is a claim by the community, recorded as such -- it is
#: evidence, and it is checked against the licence vocabulary, but it is
#: not a legal verdict and does not bypass the rights engine.
_PD_TEMPLATES = (
    "{{pd-old", "{{pd-us", "{{domaine public", "{{pd/",
    "{{public domain", "{{pd-anon", "{{pd-1923", "{{pd-old-70",
)


def _work_licence_from_wikitext(wikitext: str) -> tuple[str, str]:
    """
    Read a public-domain template, if the page carries one.

    Returns ("", "") when it does not -- which leaves the work layer
    UNKNOWN and quarantines the document. That is the intended outcome:
    "the dump did not say" is not "the dump said yes".
    """
    lowered = (wikitext or "").lower()
    for template in _PD_TEMPLATES:
        if template in lowered:
            return (
                "This work is in the public domain.",
                f"public-domain template {template.strip('{')!r} on the page",
            )
    return "", ""


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------


@register_adapter
class WikisourceDumpAdapter(SourceAdapter):
    """
    Options:
      `wiki`          dump name, e.g. "frwikisource" (required)
      `language`      ISO code for the wiki's language
      `mode`          "dump-seed" (default) or "full"
      `max_works`     seed ceiling, clamped to 20..50
      `transport`     "xml_export" (default) or "enterprise"
      `dump_date`     pin a release: "20260701". Empty means newest.
      `not_after`     resolve the newest release at or before this date
    """

    adapter_name = "wikisource_dump"
    capabilities = WIKISOURCE_DUMP_CAPABILITIES

    def __init__(self, config, fetcher) -> None:
        super().__init__(config, fetcher)
        #: Pages whose text lives in the `Page:` namespace and so is not
        #: in this dump. Counted so a run can report "found 800 works,
        #: 12000 more exist whose text this dump does not carry" instead
        #: of leaving that gap invisible.
        self.shells_seen = 0

    # ------------------------------------------------------------ config

    @property
    def wiki(self) -> str:
        wiki = str(self.config.option("wiki", "") or "").strip()
        if not wiki or not wiki.isalnum():
            raise DumpError(
                f"{self.config.source_id}: option 'wiki' must be a dump name "
                f"like 'frwikisource', got {wiki!r}"
            )
        return wiki

    @property
    def transport(self) -> str:
        return str(self.config.option("transport", "xml_export"))

    def enterprise_token(self) -> str:
        return os.environ.get(ENTERPRISE_TOKEN_ENV, "").strip()

    def seed_limit(self) -> int:
        """
        The seed ceiling, clamped rather than trusted.

        A configuration file asking for 5000 works in seed mode has
        misunderstood the mode, and honouring it would start an
        unattended multi-gigabyte harvest under a name that promises the
        opposite.
        """
        requested = int(self.config.option("max_works", SEED_MAX_WORKS) or SEED_MAX_WORKS)
        clamped = max(SEED_MIN_WORKS, min(SEED_MAX_WORKS, requested))
        if clamped != requested:
            logger.warning(
                "%s: max_works=%d is outside the %d-%d seed range; using %d",
                self.config.source_id, requested, SEED_MIN_WORKS, SEED_MAX_WORKS, clamped,
            )
        return clamped

    # -------------------------------------------------------- resolution

    def resolve_release(self) -> DumpRelease:
        """
        Find a dated release and read its manifest.

        `latest/` is never used. It is a symlink whose target moves, and
        a run that resolves it twice -- once for the index, once for the
        data -- can read an index from one release against a dump from
        the next, which produces byte offsets pointing into the middle of
        the wrong stream.
        """
        wiki = self.wiki
        pinned = str(self.config.option("dump_date", "") or "").strip()

        if pinned:
            date = pinned
        else:
            listing = self.fetch_metadata(f"{DUMPS_BASE}/{wiki}/", conditional=False)
            dates = parse_dump_dates(listing.content.decode("utf-8", errors="replace"))
            date = latest_complete_date(
                dates, not_after=str(self.config.option("not_after", "") or "")
            )

        status = self.fetch_metadata(f"{DUMPS_BASE}/{wiki}/{date}/dumpstatus.json", conditional=False)
        return parse_dumpstatus(status.content, wiki=wiki, date=date)

    # --------------------------------------------------------- discovery

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        if self.transport == "enterprise" and not self.enterprise_token():
            raise DumpError(
                f"{self.config.source_id}: transport 'enterprise' needs "
                f"${ENTERPRISE_TOKEN_ENV}. Unset it or use transport 'xml_export', "
                "which needs no credentials."
            )

        release = self.resolve_release()
        index_file = release.find("pages-articles-multistream-index", ".txt.bz2")
        data_file = release.find("pages-articles-multistream.xml.bz2")

        entries = self._read_index(index_file)

        limit = self.seed_limit()
        start_offset, last_page_id = parse_checkpoint(cursor or "")
        ranges = [
            r for r in stream_ranges(entries, total_bytes=data_file.size)
            if r.offset >= start_offset
        ]
        if not ranges:
            return DiscoveryResult(candidates=[], exhausted=True)

        candidates: list[Candidate] = []
        checkpoint = ""
        streams_read = 0
        for stream in ranges:
            if len(candidates) >= limit:
                break
            pages = self._read_stream(data_file.absolute_url(DUMPS_BASE), stream)
            streams_read += 1
            for page in pages:
                if page.page_id <= last_page_id and stream.offset == start_offset:
                    continue  # already imported before the interruption
                candidate = self._candidate_for(page, release)
                if candidate is not None:
                    candidates.append(candidate)
                checkpoint = checkpoint_token(stream.offset, page.page_id)
                if len(candidates) >= limit:
                    break

        logger.info(
            "%s %s: %d works from %d stream(s) -- %d bytes read of a %d-byte dump; "
            "%d page(s) skipped whose text is in the Page: namespace",
            release.wiki, release.date, len(candidates), streams_read,
            sum(r.length for r in ranges[:streams_read]), data_file.size, self.shells_seen,
        )
        exhausted = len(candidates) < limit
        return DiscoveryResult(
            candidates=candidates,
            next_cursor=None if exhausted else checkpoint,
            exhausted=exhausted,
        )

    def _read_index(self, index_file) -> list[IndexEntry]:
        """
        Read the multistream index -- a prefix of it, in seed mode.

        Found by the live pilot: frwikisource's index is 28 MB compressed
        and expands past 200 MB. Downloading and buffering all of it to
        take thirty titles is the same mistake as downloading the 2.8 GB
        dump to read fifty pages, one layer down. Seed mode Range-fetches
        a prefix instead; the first 300 kB of the real index held 29,126
        entries across 292 streams, which is three orders of magnitude
        more than seed mode can use.

        Archive mode reads the whole index, because it genuinely needs
        every offset.
        """
        url = index_file.absolute_url(DUMPS_BASE)
        if self.config.option("mode", "dump-seed") != "dump-seed":
            response = self.fetcher.fetch(
                url,
                allowed_hosts=self.config.allowed_hosts,
                max_bytes=self.config.max_download_bytes,
            )
            return parse_multistream_index(_index_text(response.content, whole=True))

        prefix_bytes = int(self.config.option("index_prefix_bytes", INDEX_PREFIX_BYTES))
        if index_file.size and index_file.size <= prefix_bytes:
            response = self.fetcher.fetch(
                url, allowed_hosts=self.config.allowed_hosts, max_bytes=self.config.max_download_bytes
            )
        else:
            response = self.fetcher.fetch(
                url,
                allowed_hosts=self.config.allowed_hosts,
                max_bytes=self.config.max_download_bytes,
                byte_range=f"bytes=0-{prefix_bytes - 1}",
            )

        entries = parse_multistream_index(_index_text(response.content))
        if not entries:
            raise DumpError(
                f"{url}: the first {prefix_bytes} bytes of the index yielded no entries"
            )
        logger.info(
            "%s: %d index entries from a %d-byte prefix of a %d-byte index",
            index_file.name, len(entries), len(response.content), index_file.size,
        )
        return entries

    def _read_stream(self, url: str, stream: StreamRange) -> list[DumpPage]:
        """
        Fetch and decompress one bzip2 stream by byte range.

        A stream that fails is skipped, not fatal: one unreadable stream
        out of a dump's tens of thousands must not end the run, and the
        checkpoint means a later run resumes past it rather than
        repeating it forever.
        """
        try:
            response = self.fetcher.fetch(
                url,
                allowed_hosts=self.config.allowed_hosts,
                max_bytes=self.config.max_download_bytes,
                byte_range=stream.header(),
            )
            raw = decompress_stream(response.content)
            return parse_pages(wrap_stream_fragment(raw))
        except Exception as exc:  # noqa: BLE001 -- per-stream containment
            logger.info("stream at offset %d unreadable (%s); skipping", stream.offset, exc)
            return []

    def _candidate_for(self, page: DumpPage, release: DumpRelease) -> Candidate | None:
        if not page.is_content:
            return None

        language = normalize_language_code(
            str(self.config.option("language", "") or release.wiki[:2])
        )
        text, _ = clean_wikitext(page.wikitext)
        importable, reason = dump_importable(page, text)
        if not importable:
            if reason.startswith("transclusion shell"):
                # Worth counting rather than dropping in silence: it is
                # the single most common outcome on a Wikisource dump,
                # and an operator seeing "0 works" deserves to know it
                # was the Page: namespace and not a broken parser.
                self.shells_seen += 1
            logger.debug("%s: not importable (%s)", page.title, reason)
            return None

        page_url = f"https://{language}.wikisource.org/wiki/{page.title.replace(' ', '_')}"
        stack = rights_stack_for(page, page_url=page_url, language=language)
        work = stack.get(RightsComponent.WORK)

        return Candidate(
            source_id=self.config.source_id,
            external_id=f"{release.wiki}:{page.page_id}",
            title=page.title,
            authors=(),
            language=language,
            edition_date=page.timestamp,
            document_type="text",
            canonical_url=page_url,
            # No download URL: the text came out of the dump with the
            # metadata, in the same pass. Re-fetching the page over HTTP
            # would be a second request for bytes already held.
            download_url="",
            download_format="wikitext",
            estimated_bytes=len(page.wikitext.encode("utf-8")),
            rights=RightsSignal(
                content_license=CC_BY_SA_URL,
                provider_declared_scope="",
                evidence=work.evidence if work else (),
                raw_rights_text=f"CC BY-SA 4.0 (site) + {work.notes if work else 'unknown work'}",
            ),
            raw_metadata={
                "dump": f"{release.wiki}/{release.date}",
                "revision_id": page.revision_id,
                "page_id": page.page_id,
                "namespace": page.namespace,
                "rights_components": stack.to_dict(),
                "text": text,
            },
        )

    def fetch_content(self, candidate: Candidate, max_bytes: int = 0):
        """
        The text is already here.

        Discovery read it out of the dump stream along with the metadata,
        so this returns those bytes rather than issuing a request. A
        second HTTP call for text already in hand would be the one thing
        the dump route exists to avoid.
        """
        from ..http import FetchResponse
        from ..security import sha256_bytes

        text = (candidate.raw_metadata or {}).get("text", "")
        if not text.strip():
            raise ValueError(f"{candidate.external_id}: the dump carried no text for this page")

        payload = text.encode("utf-8")
        return FetchResponse(
            url=candidate.canonical_url,
            final_url=candidate.canonical_url,
            status_code=200,
            content=payload,
            headers={},
            detected_mime="text/plain",
            declared_mime="text/plain",
            encoding="utf-8",
            sha256=sha256_bytes(payload),
        )


def _index_text(payload: bytes, *, whole: bool = False) -> str:
    """
    The index as text, whether it arrived compressed or not.

    Sniffing the bzip2 magic rather than trusting the URL suffix: a
    mirror that serves the file already decompressed is a real thing, and
    handing bz2-decompression a plain text file produces an empty index
    and a run that finds nothing while reporting no error.

    `whole=False` (the default) tolerates a truncated tail, because in
    seed mode the payload is deliberately a prefix.
    """
    if payload[:3] != b"BZh":
        return payload.decode("utf-8", errors="replace")
    if whole:
        return decompress_stream(payload, max_bytes=MAX_INDEX_BYTES).decode("utf-8", errors="replace")
    return decompress_prefix(payload).decode("utf-8", errors="replace")


def seed_titles(entries: list[IndexEntry], limit: int) -> list[IndexEntry]:
    """
    The first `limit` entries that are not obviously namespaced.

    A convenience for planning and for the CLI's dry run; discovery
    itself filters on `<ns>` from the dump, which is authoritative.
    """
    from ..dumps import looks_namespaced

    return [e for e in entries if not looks_namespaced(e.title)][:limit]


__all__ = [
    "CC_BY_SA_URL",
    "INDEX_PREFIX_BYTES",
    "MAX_INDEX_BYTES",
    "MIN_DUMP_CONTENT_CHARS",
    "ENTERPRISE_TOKEN_ENV",
    "MAIN_NAMESPACE",
    "SEED_MAX_WORKS",
    "SEED_MIN_WORKS",
    "WIKISOURCE_DUMP_CAPABILITIES",
    "DumpPage",
    "WikisourceDumpAdapter",
    "dump_importable",
    "is_transclusion_shell",
    "parse_pages",
    "rights_stack_for",
    "seed_titles",
]
