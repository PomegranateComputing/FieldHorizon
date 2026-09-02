"""
Project Gutenberg, via its official catalogue -- never by crawling.

Project Gutenberg's terms of use are explicit that the site must not be
crawled, and it publishes a complete catalogue precisely so that nobody
needs to. This adapter reads that catalogue (the RDF/XML per-book records
distributed in the catalog archive, or the CSV summary), and downloads
individual texts from the official mirror paths at a configured rate,
with a User-Agent carrying a contact address.

**The jurisdiction point is the important one.** Project Gutenberg
determines public-domain status *under United States law* and says so.
"Available on Project Gutenberg" therefore establishes
`public-domain-us`, never worldwide public domain. Under
`local_research_us` that is an accept marked `LOCAL_US_ONLY`; under
`release_worldwide` it is a quarantine. Translating a Gutenberg listing
into a worldwide public-domain claim is exactly the inference the brief
forbids, and the licence vocabulary is built so the mistake cannot be
made silently -- `LIC_GUTENBERG_US` has `inherent_scope =
LOCAL_US_ONLY` and no code path can widen it.

Format preference is TXT (UTF-8) → EPUB → HTML, per the brief.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import tempfile
from dataclasses import replace
from pathlib import Path

from ..http import NotModified
from ..language import normalize_language_code
from ..models import Candidate, RightsSignal
from ..normalization import rank_format
from ..rights import evidence_from_field
from ..security import local_name, safe_parse_xml
from .base import DiscoveryResult, SourceAdapter, register_adapter

logger = logging.getLogger(__name__)

#: Project Gutenberg's own statement about the basis of its
#: determinations. Recorded as evidence on every candidate.
GUTENBERG_US_STATEMENT = (
    "Project Gutenberg determines public domain status under the copyright law of the "
    "United States. Works are free to use in the United States; status elsewhere is not "
    "established by this listing."
)

DEFAULT_LICENSE_URL = "https://www.gutenberg.org/policy/permission.html"

#: Gutenberg's `rights` field value for public-domain texts.
_PD_US_MARKERS = (
    "public domain in the usa",
    "public domain in the united states",
    "public domain (us)",
)

_FORMAT_BY_MIME = {
    "text/plain; charset=utf-8": "txt",
    "text/plain; charset=us-ascii": "txt",
    "text/plain": "txt",
    "application/epub+zip": "epub",
    "text/html; charset=utf-8": "html",
    "text/html": "html",
    "application/rdf+xml": "",
    "application/x-mobipocket-ebook": "mobi",
}


def parse_gutenberg_rdf(xml_bytes: bytes, source_id: str) -> Candidate | None:
    """
    Parse one `pg<N>.rdf` record from the Gutenberg catalogue archive.

    Returns None for a record with no downloadable text format -- audio
    books and pure-image records exist in the catalogue and are not this
    corpus's business.
    """
    root = safe_parse_xml(xml_bytes)

    ebook = None
    for element in root.iter():
        if local_name(element.tag) == "ebook":
            ebook = element
            break
    if ebook is None:
        return None

    about = ""
    for key, value in ebook.attrib.items():
        if local_name(key) == "about":
            about = value
            break
    match = re.search(r"(\d+)", about)
    if not match:
        return None
    book_id = match.group(1)

    title = ""
    language = ""
    rights_text = ""
    publication_date = ""
    subjects: list[str] = []
    authors: list[str] = []
    document_type = ""

    for element in ebook.iter():
        name = local_name(element.tag).lower()
        text = " ".join((element.text or "").split())
        if name == "title" and text and not title:
            title = text
        elif name == "rights" and text and not rights_text:
            rights_text = text
        elif name == "issued" and text and not publication_date:
            publication_date = text
        elif name == "type" and text and not document_type:
            document_type = text

    # Language sits inside dcterms:language/rdf:Description/rdf:value.
    for element in ebook.iter():
        if local_name(element.tag).lower() == "language":
            for child in element.iter():
                if local_name(child.tag).lower() == "value" and child.text:
                    language = normalize_language_code(child.text)
                    break
        if language:
            break

    # Subjects: dcterms:subject/rdf:Description/rdf:value. LCSH and LCC
    # entries both appear; both are useful classification signal.
    for element in ebook.iter():
        if local_name(element.tag).lower() == "subject":
            for child in element.iter():
                if local_name(child.tag).lower() == "value" and child.text:
                    value = " ".join(child.text.split())
                    if value and value not in subjects:
                        subjects.append(value)

    # Creators are pgterms:agent/pgterms:name, possibly several.
    for element in root.iter():
        if local_name(element.tag).lower() == "agent":
            for child in element.iter():
                if local_name(child.tag).lower() == "name" and child.text:
                    name_value = " ".join(child.text.split())
                    if name_value and name_value not in authors:
                        authors.append(name_value)

    downloads: list[tuple[str, str, int]] = []
    for element in root.iter():
        if local_name(element.tag).lower() != "file":
            continue
        url = ""
        for key, value in element.attrib.items():
            if local_name(key) == "about":
                url = value
                break
        if not url:
            continue
        size = 0
        fmt = ""
        for child in element.iter():
            child_name = local_name(child.tag).lower()
            if child_name == "extent" and child.text and child.text.strip().isdigit():
                size = int(child.text.strip())
            elif child_name == "value" and child.text:
                fmt = _FORMAT_BY_MIME.get(child.text.strip().lower(), fmt)
        if not fmt:
            fmt = _format_from_url(url)
        # Zipped derivatives add nothing over the plain file and cost a
        # decompression step; the plain form is always offered too.
        if fmt and not url.endswith(".zip"):
            downloads.append((fmt, url, size))

    downloads.sort(key=lambda d: rank_format(d[0]))
    if not downloads:
        return None

    scope = "LOCAL_US_ONLY" if _is_us_public_domain(rights_text) else ""
    canonical_url = f"https://www.gutenberg.org/ebooks/{book_id}"

    evidence = [
        evidence_from_field("gutenberg:rdf:rights", rights_text or "(no rights field)", canonical_url),
        evidence_from_field("gutenberg:jurisdiction-statement", GUTENBERG_US_STATEMENT, DEFAULT_LICENSE_URL),
    ]

    return Candidate(
        source_id=source_id,
        external_id=book_id,
        title=title,
        authors=tuple(authors),
        language=language,
        publication_date=publication_date,
        document_type=document_type,
        subjects=tuple(subjects[:24]),
        canonical_url=canonical_url,
        download_url=downloads[0][1],
        download_format=downloads[0][0],
        estimated_bytes=downloads[0][2],
        alternate_downloads=tuple((fmt, url) for fmt, url, _ in downloads[1:]),
        work_identifiers=(f"gutenberg:{book_id}",),
        rights=RightsSignal(
            content_license=rights_text,
            provider_declared_scope=scope,
            jurisdictions=("US",) if scope else (),
            evidence=tuple(evidence),
            raw_rights_text=rights_text,
        ),
        raw_metadata={
            "gutenberg_id": book_id,
            "title": title,
            "authors": authors,
            "subjects": subjects[:24],
            "rights": rights_text,
            "downloads": [{"format": f, "url": u, "size": s} for f, u, s in downloads],
        },
    )


def _is_us_public_domain(rights_text: str) -> bool:
    low = (rights_text or "").lower()
    return any(marker in low for marker in _PD_US_MARKERS)


def _format_from_url(url: str) -> str:
    low = url.lower()
    for suffix, fmt in ((".txt", "txt"), (".epub", "epub"), (".html", "html"), (".htm", "html"), (".pdf", "pdf")):
        if low.endswith(suffix):
            return fmt
    if re.search(r"/\d+\.txt\.utf-?8$", low):
        return "txt"
    return ""


def parse_gutenberg_csv(csv_bytes: bytes, source_id: str) -> list[Candidate]:
    """
    Parse the official `pg_catalog.csv` summary.

    The CSV carries no per-format download URLs, so those are constructed
    from the documented canonical paths rather than guessed: the plain
    text at `/ebooks/<id>.txt.utf-8` and EPUB at `/ebooks/<id>.epub3.images`.
    A record whose rights field is not a US public-domain statement is
    still emitted -- the rights engine, not this parser, decides what
    happens to it.
    """
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    candidates: list[Candidate] = []

    for row in reader:
        book_id = (row.get("Text#") or "").strip()
        if not book_id.isdigit():
            continue
        if (row.get("Type") or "Text").strip() not in ("Text", ""):
            continue

        rights_text = (row.get("Rights") or "").strip()
        title = " ".join((row.get("Title") or "").split())
        authors = [a.strip() for a in (row.get("Authors") or "").split(";") if a.strip()]
        subjects = [s.strip() for s in (row.get("Subjects") or "").split(";") if s.strip()]
        language = normalize_language_code((row.get("Language") or "").split(";")[0])
        canonical_url = f"https://www.gutenberg.org/ebooks/{book_id}"

        scope = "LOCAL_US_ONLY" if _is_us_public_domain(rights_text) else ""

        candidates.append(
            Candidate(
                source_id=source_id,
                external_id=book_id,
                title=title,
                authors=tuple(authors),
                language=language,
                publication_date=(row.get("Issued") or "").strip(),
                document_type="Text",
                subjects=tuple(subjects[:24]),
                canonical_url=canonical_url,
                download_url=f"https://www.gutenberg.org/ebooks/{book_id}.txt.utf-8",
                download_format="txt",
                alternate_downloads=(
                    ("epub", f"https://www.gutenberg.org/ebooks/{book_id}.epub3.images"),
                    ("html", f"https://www.gutenberg.org/ebooks/{book_id}.html.images"),
                ),
                work_identifiers=(f"gutenberg:{book_id}",),
                rights=RightsSignal(
                    content_license=rights_text,
                    provider_declared_scope=scope,
                    jurisdictions=("US",) if scope else (),
                    evidence=(
                        evidence_from_field("gutenberg:csv:rights", rights_text or "(empty)", canonical_url),
                        evidence_from_field(
                            "gutenberg:jurisdiction-statement", GUTENBERG_US_STATEMENT, DEFAULT_LICENSE_URL
                        ),
                    ),
                    raw_rights_text=rights_text,
                ),
                raw_metadata={k: v for k, v in row.items() if v},
            )
        )

    return candidates


@register_adapter
class GutenbergAdapter(SourceAdapter):
    """
    Options:
      `catalog_url`  -- the official CSV catalogue URL
      `page_size`    -- candidates emitted per discovery page (default 200)

    The catalogue is fetched conditionally (ETag/Last-Modified), so a
    re-run against an unchanged catalogue costs one 304 rather than a
    full re-download -- which is both polite and what makes repeated
    `corpus sync` runs cheap.
    """

    adapter_name = "gutenberg"

    def __init__(self, config, fetcher) -> None:
        super().__init__(config, fetcher)
        #: The parsed catalogue, held for this adapter's lifetime.
        self._catalogue: list[Candidate] | None = None

    def _catalogue_cache_path(self, catalog_url: str) -> Path:
        """
        Where the fetched catalogue body is kept between runs.

        Keyed by a hash of the URL, not by source id: two sources sharing
        one catalogue should share one cached copy, which is the whole
        point.
        """
        digest = hashlib.sha256(catalog_url.encode("utf-8")).hexdigest()[:16]
        root = self.config.cache_dir or Path(tempfile.gettempdir()) / "fieldhorizon-corpus-cache"
        return root / f"catalogue_{digest}.csv"

    def _load_catalogue(self) -> list[Candidate]:
        """
        Fetch and parse the catalogue once per run, not once per page.

        The whole catalogue is a single CSV, so paging through it must
        not re-fetch it: the second conditional request necessarily
        returns 304, and NotModified propagating out of page two aborted
        discovery after a single page while reporting success.

        A language filter is applied here rather than after paging, so
        `page_size` counts documents we actually want -- with per-item
        RDF enrichment costing one request each, paging through
        thousands of unwanted rows would be pure waste.
        """
        if self._catalogue is not None:
            return self._catalogue

        catalog_url = self.config.option(
            "catalog_url", "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"
        )
        cache_path = self._catalogue_cache_path(catalog_url)

        try:
            response = self.fetch_metadata(catalog_url, conditional=True)
            raw = response.content
            # Keep the body. A conditional request is only worth making
            # if a 304 can be answered from somewhere -- and the
            # catalogue is tens of megabytes, so re-downloading it once
            # per source is exactly what we are trying to avoid.
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(raw)
            except OSError as exc:
                logger.info("%s: could not cache the catalogue (%s)", self.config.source_id, exc)
        except NotModified:
            # Unchanged since it was last fetched -- which, when two
            # sources share one catalogue URL, routinely means the OTHER
            # source fetched it moments ago. Serving the cached body is
            # what makes a second Gutenberg source (a language-filtered
            # one, say) work at all; without it the second source
            # silently discovered nothing.
            if not cache_path.exists():
                logger.warning(
                    "%s: catalogue returned 304 but no cached copy exists; discovering nothing this run",
                    self.config.source_id,
                )
                self._catalogue = []
                return []
            logger.info("%s: catalogue unchanged; using the cached copy", self.config.source_id)
            raw = cache_path.read_bytes()

        candidates = parse_gutenberg_csv(raw, self.config.source_id)

        languages = [str(code).lower() for code in (self.config.option("filter_languages", []) or [])]
        if languages:
            candidates = [c for c in candidates if (c.language or "").lower() in languages]

        self._catalogue = candidates
        return candidates

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        offset = int(cursor) if cursor and cursor.isdigit() else 0
        page_size = int(self.config.option("page_size", 25))

        all_candidates = self._load_catalogue()
        if not all_candidates:
            return DiscoveryResult(candidates=[], next_cursor=None, exhausted=True)

        page = all_candidates[offset : offset + page_size]
        if self.config.option("fetch_rights_rdf", True):
            page = [self._with_rdf_rights(c) for c in page]

        next_offset = offset + page_size
        exhausted = next_offset >= len(all_candidates)

        return DiscoveryResult(
            candidates=page,
            next_cursor=None if exhausted else str(next_offset),
            exhausted=exhausted,
        )

    def _with_rdf_rights(self, candidate: Candidate) -> Candidate:
        """
        Fetch one book's official RDF record for its per-item rights.

        The published `pg_catalog.csv` carries no rights column at all --
        Text#, Type, Issued, Title, Language, Authors, Subjects, LoCC,
        Bookshelves and nothing more. Deciding rights from it alone is
        therefore impossible, and the rights engine correctly quarantines
        every candidate built from the CSV on its own.

        Gutenberg publishes a small per-book RDF at /ebooks/<id>.rdf
        carrying `dcterms:rights`, which is the real per-item statement.
        One extra request per candidate is the honest cost of having
        actual evidence rather than an assumption -- which is why
        `page_size` defaults low for this source.

        A failed fetch leaves the candidate exactly as the CSV described
        it: no rights statement, and therefore a quarantine. Failing to
        confirm is never the same as confirming.
        """
        rdf_url = f"https://www.gutenberg.org/ebooks/{candidate.external_id}.rdf"
        try:
            # NOT conditional. A 304 would be answered from a cache we
            # do not keep for per-item records, so enrichment would
            # silently not happen -- and a candidate with no rights is a
            # candidate that gets quarantined. On the second run that
            # meant every Gutenberg document quietly stopped qualifying.
            response = self.fetch_metadata(rdf_url, conditional=False, accept="application/rdf+xml")
        except Exception as exc:
            logger.info(
                "Gutenberg %s: per-item RDF unavailable (%s); candidate keeps its unlicensed state",
                candidate.external_id, exc,
            )
            return candidate

        try:
            enriched = parse_gutenberg_rdf(response.content, self.config.source_id)
        except Exception as exc:
            logger.info("Gutenberg %s: RDF unparseable (%s)", candidate.external_id, exc)
            return candidate
        if enriched is None:
            return candidate

        # The RDF supplies rights and per-format downloads; the CSV's
        # richer subject list is kept, since it feeds classification and
        # the curator's thematic scoring.
        return replace(
            enriched,
            subjects=candidate.subjects or enriched.subjects,
            title=enriched.title or candidate.title,
        )


__all__ = [
    "DEFAULT_LICENSE_URL",
    "GUTENBERG_US_STATEMENT",
    "GutenbergAdapter",
    "parse_gutenberg_csv",
    "parse_gutenberg_rdf",
]
