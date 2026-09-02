"""
OPDS 1.x (Atom) catalogue adapter.

OPDS is the reusable brick: any library exposing an OPDS feed can be
added as configuration alone, with no new code. Standard Ebooks is the
first consumer; the parser is deliberately generic.

Namespaces are matched by local element name rather than by URI. Real
OPDS feeds in the wild disagree about namespace URIs constantly (Atom
vs. the OPDS extension vs. Dublin Core vs. schema.org), and a hardcoded
namespace map is how an adapter silently returns zero entries against a
perfectly valid feed.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

from ..language import normalize_language_code
from ..models import Candidate, Evidence, RightsSignal
from ..normalization import rank_format
from ..rights import evidence_from_field
from ..security import child_local, children_local, local_name, safe_parse_xml
from .base import DiscoveryResult, SourceAdapter, register_adapter

logger = logging.getLogger(__name__)

#: Link relations that point at downloadable content rather than at
#: navigation, cover art, or catalogue pages.
ACQUISITION_RELS = frozenset(
    {
        "http://opds-spec.org/acquisition",
        "http://opds-spec.org/acquisition/open-access",
        "http://opds-spec.org/acquisition/buy",
        "http://opds-spec.org/acquisition/borrow",
        "http://opds-spec.org/acquisition/subscribe",
        "http://opds-spec.org/acquisition/sample",
    }
)

#: The only acquisition relations that mean "you may simply download
#: this". `buy`, `borrow`, and `subscribe` are access-restricted by
#: definition and are recorded as such rather than followed.
OPEN_ACCESS_RELS = frozenset(
    {
        "http://opds-spec.org/acquisition",
        "http://opds-spec.org/acquisition/open-access",
        # Plain Atom syndication feeds (Standard Ebooks' public
        # new-releases feed among them) use rel="enclosure" rather than
        # an OPDS acquisition relation. It means the same thing -- here
        # is the file -- and omitting it made a perfectly valid official
        # feed yield entries with no download at all.
        "enclosure",
    }
)

RESTRICTED_RELS = frozenset(
    {
        "http://opds-spec.org/acquisition/buy",
        "http://opds-spec.org/acquisition/borrow",
        "http://opds-spec.org/acquisition/subscribe",
    }
)

_MIME_TO_FORMAT = {
    "text/plain": "txt",
    "application/epub+zip": "epub",
    "application/pdf": "pdf",
    "text/html": "html",
    "application/xhtml+xml": "xhtml",
}

#: Formats deliberately NOT mapped above, and why: `mobi`/`azw3` and
#: `kepub` are reader-specific repackagings of the same text, so
#: downloading one costs bandwidth for no additional content. A link
#: whose type is not in the map is skipped rather than ranked low.


def parse_opds_feed(xml_bytes: bytes, base_url: str, source_id: str) -> tuple[list[Candidate], str | None]:
    """
    Parse one OPDS page into candidates plus the `rel="next"` URL.

    Returns ([], None) for a pure navigation feed with no entries -- that
    is a valid OPDS document, not an error.
    """
    root = safe_parse_xml(xml_bytes)

    next_url: str | None = None
    for link in children_local(root, "link"):
        if link.attrib.get("rel") == "next":
            href = link.attrib.get("href", "")
            if href:
                next_url = urljoin(base_url, href)
            break

    candidates: list[Candidate] = []
    for entry in children_local(root, "entry"):
        candidate = _parse_entry(entry, base_url, source_id)
        if candidate is not None:
            candidates.append(candidate)

    return candidates, next_url


def _text_of(element, name: str) -> str:
    child = child_local(element, name)
    if child is None or child.text is None:
        return ""
    return " ".join(child.text.split())


def _parse_entry(entry, base_url: str, source_id: str) -> Candidate | None:
    title = _text_of(entry, "title")
    external_id = _text_of(entry, "id")
    if not external_id:
        # An OPDS entry with no id cannot be tracked idempotently across
        # runs; skipping is safer than minting a synthetic id that would
        # change whenever the feed reorders.
        logger.debug("Skipping OPDS entry with no <id>: %r", title)
        return None

    authors: list[str] = []
    contributors: list[str] = []
    for author_el in children_local(entry, "author"):
        name = _text_of(author_el, "name")
        if name:
            authors.append(name)
    for contributor_el in children_local(entry, "contributor"):
        name = _text_of(contributor_el, "name")
        if name:
            contributors.append(name)

    subjects: list[str] = []
    for category in children_local(entry, "category"):
        label = category.attrib.get("label") or category.attrib.get("term") or ""
        if label:
            subjects.append(label)

    language = ""
    document_type = ""
    for child in entry:
        name = local_name(child.tag).lower()
        if name == "language" and child.text:
            language = normalize_language_code(child.text)
        elif name in ("type", "format") and child.text and not document_type:
            document_type = child.text.strip()

    canonical_url = ""
    downloads: list[tuple[str, str, int]] = []  # (format, url, size)
    restricted = False
    for link in children_local(entry, "link"):
        rel = link.attrib.get("rel", "")
        href = link.attrib.get("href", "")
        mime = (link.attrib.get("type", "") or "").split(";")[0].strip().lower()
        if not href:
            continue
        absolute = urljoin(base_url, href)

        if rel in ("alternate", "self") and mime in ("text/html", "application/atom+xml;type=entry"):
            canonical_url = canonical_url or absolute
        if rel in RESTRICTED_RELS:
            restricted = True
            continue
        if rel in OPEN_ACCESS_RELS or (not rel and mime in _MIME_TO_FORMAT):
            fmt = _MIME_TO_FORMAT.get(mime, "")
            if not fmt:
                continue
            try:
                size = int(link.attrib.get("length", "0"))
            except ValueError:
                size = 0
            downloads.append((fmt, absolute, size))

    downloads.sort(key=lambda d: rank_format(d[0]))

    rights_text = _text_of(entry, "rights")
    rights_uri = ""
    for child in entry:
        if local_name(child.tag).lower() in ("license", "rights", "usageterms"):
            href = child.attrib.get("href", "") or child.attrib.get("resource", "")
            if href:
                rights_uri = href
                break

    evidence: list[Evidence] = []
    if rights_text:
        evidence.append(
            evidence_from_field("opds:rights", rights_text, canonical_url or base_url)
        )
    if rights_uri:
        evidence.append(evidence_from_field("opds:license-link", rights_uri, canonical_url or base_url))

    summary = _text_of(entry, "summary") or _text_of(entry, "content")

    return Candidate(
        source_id=source_id,
        external_id=external_id,
        title=title,
        authors=tuple(authors),
        contributors=tuple(contributors),
        language=language,
        publication_date=_text_of(entry, "published") or _text_of(entry, "issued"),
        edition_date=_text_of(entry, "updated"),
        document_type=document_type,
        subjects=tuple(subjects[:24]),
        canonical_url=canonical_url,
        download_url=downloads[0][1] if downloads else "",
        download_format=downloads[0][0] if downloads else "",
        estimated_bytes=downloads[0][2] if downloads else 0,
        alternate_downloads=tuple((fmt, url) for fmt, url, _ in downloads[1:]),
        rights=RightsSignal(
            content_license=rights_text,
            rights_statement_uri=rights_uri,
            evidence=tuple(evidence),
            access_restricted=restricted and not downloads,
            raw_rights_text=rights_text,
        ),
        raw_metadata={
            "title": title,
            "id": external_id,
            "summary": summary[:2000],
            "subjects": subjects[:24],
            "downloads": [{"format": f, "url": u, "size": s} for f, u, s in downloads],
        },
    )


@register_adapter
class OPDSAdapter(SourceAdapter):
    """
    Generic OPDS walker.

    Options:
      `start_path` -- feed path appended to base_url (default the base
                      URL itself).
    """

    adapter_name = "opds"

    def start_url(self) -> str:
        start_path = self.config.option("start_path", "")
        return urljoin(self.config.base_url, start_path) if start_path else self.config.base_url

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        url = cursor or self.start_url()
        response = self.fetch_metadata(url, accept="application/atom+xml")
        candidates, next_url = parse_opds_feed(response.content, url, self.config.source_id)
        return DiscoveryResult(
            candidates=candidates,
            next_cursor=next_url,
            exhausted=next_url is None,
        )


__all__ = ["ACQUISITION_RELS", "OPEN_ACCESS_RELS", "OPDSAdapter", "parse_opds_feed"]
