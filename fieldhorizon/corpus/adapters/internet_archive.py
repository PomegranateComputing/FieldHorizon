"""
Internet Archive -- deliberately the most conservative adapter here.

Internet Archive is an extraordinary discovery surface and is **not** a
guarantor of the rights of what it hosts. Anyone can upload, and the
`licenseurl` field on an item reflects what *the uploader* asserted, not
an institutional determination. Treating that assertion as a licence is
how open-corpus projects end up redistributing copyrighted books.

So this adapter defaults to `trusted_for_content_rights = False`, which
makes the rights engine refuse to accept on IA's word alone
(`UNTRUSTED_UPLOADER_ASSERTION` → quarantine). There is exactly one way
past that: an explicit **collection allowlist** of institutional
collections whose uploads are curated by an identifiable institution. An
item in an allowlisted collection carrying an explicit open licence can
be accepted; everything else is quarantined for review.

Also refused outright, before rights are even considered:

* `access-restricted-item` / `access-restricted` — lending-library items;
* items in the `inlibrary` or `printdisabled` collections — borrow-only;
* items whose file list contains no usable text format.

Only the best text derivative is downloaded. IA items routinely carry
twenty derivative files (JP2 archives, DjVu, multiple PDFs); pulling
them all would be gigabytes of waste per book.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote, urlencode

from ..capabilities import Capability, SourceCapabilities
from ..language import normalize_language_code
from ..models import Candidate, Evidence, RightsSignal
from ..normalization import rank_format
from ..rights import evidence_from_field
from .base import DiscoveryResult, SourceAdapter, register_adapter

logger = logging.getLogger(__name__)

#: Collections that mean the item cannot be freely downloaded.
BORROW_ONLY_COLLECTIONS = frozenset({"inlibrary", "printdisabled", "internetarchivebooks", "lendinglibrary"})

#: Metadata flags marking restricted access.
_RESTRICTED_KEYS = ("access-restricted-item", "access-restricted", "no-preview")

IA_UPLOADER_NOTE = (
    "Internet Archive hosts uploads from many parties. A licence field on an item reflects "
    "the uploader's assertion unless the item belongs to a curated institutional collection."
)

_FORMAT_BY_IA_NAME = {
    "text": "txt",
    "djvu txt": "txt",
    "djvutxt": "txt",
    "epub": "epub",
    "text pdf": "pdf",
    "image container pdf": "pdf",
    "additional text pdf": "pdf",
    "html": "html",
    "djvuxml": "xml",
    "abbyy gz": "",
    "single page processed jp2 zip": "",
}


def build_search_url(base_url: str, query: str, rows: int = 50, page: int = 1, fields: list[str] | None = None) -> str:
    fields = fields or [
        "identifier", "title", "creator", "year", "date", "language", "subject",
        "licenseurl", "rights", "collection", "mediatype", "publicdate",
    ]
    params = [
        ("q", query),
        ("rows", str(rows)),
        ("page", str(page)),
        ("output", "json"),
    ]
    params.extend(("fl[]", field) for field in fields)
    return f"{base_url.rstrip('/')}/advancedsearch.php?{urlencode(params)}"


def build_metadata_url(base_url: str, identifier: str) -> str:
    return f"{base_url.rstrip('/')}/metadata/{quote(identifier, safe='')}"


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def parse_search_response(payload: bytes, source_id: str, allowed_collections: list[str]) -> tuple[list[Candidate], int, int]:
    """
    Returns (candidates, total_found, rows_returned).

    Candidates from the search API carry no download URL: the file list
    requires the per-item metadata call, which `fetch_content` performs
    lazily -- so a broad discovery pass costs one request per page, not
    one per item.
    """
    data = json.loads(payload.decode("utf-8", errors="replace"))
    response = data.get("response", {}) or {}
    docs = response.get("docs", []) or []
    total = int(response.get("numFound", 0) or 0)

    allowlist = {c.lower() for c in (allowed_collections or [])}
    candidates: list[Candidate] = []

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        candidate = _parse_doc(doc, source_id, allowlist)
        if candidate is not None:
            candidates.append(candidate)

    return candidates, total, len(docs)


def _parse_doc(doc: dict, source_id: str, allowlist: set[str]) -> Candidate | None:
    identifier = str(doc.get("identifier") or "").strip()
    if not identifier:
        return None

    collections = [c.lower() for c in _as_list(doc.get("collection"))]
    borrow_only = bool(set(collections) & BORROW_ONLY_COLLECTIONS)

    license_url = ""
    for value in _as_list(doc.get("licenseurl")):
        license_url = value
        break
    rights_text = " | ".join(_as_list(doc.get("rights")))

    in_curated_collection = bool(allowlist & set(collections))
    canonical_url = f"https://archive.org/details/{identifier}"

    evidence: list[Evidence] = [
        evidence_from_field("internet_archive:uploader-note", IA_UPLOADER_NOTE, canonical_url),
    ]
    if license_url:
        evidence.append(evidence_from_field("internet_archive:licenseurl", license_url, canonical_url))
    if rights_text:
        evidence.append(evidence_from_field("internet_archive:rights", rights_text, canonical_url))
    if in_curated_collection:
        matched = sorted(allowlist & set(collections))
        evidence.append(
            evidence_from_field(
                "internet_archive:curated-collection",
                f"Item belongs to allowlisted institutional collection(s): {', '.join(matched)}",
                canonical_url,
            )
        )

    languages = _as_list(doc.get("language"))
    dates = _as_list(doc.get("year")) or _as_list(doc.get("date"))

    return Candidate(
        source_id=source_id,
        external_id=identifier,
        title=" ".join(str(doc.get("title") or "").split()),
        authors=tuple(_as_list(doc.get("creator"))),
        language=normalize_language_code(languages[0]) if languages else "",
        publication_date=dates[0] if dates else "",
        document_type=str(doc.get("mediatype") or ""),
        subjects=tuple(_as_list(doc.get("subject"))[:24]),
        canonical_url=canonical_url,
        # Resolved lazily from the file list -- see fetch_content.
        download_url="",
        work_identifiers=(f"ia:{identifier}",),
        rights=RightsSignal(
            content_license=license_url or rights_text,
            rights_statement_uri=license_url,
            evidence=tuple(evidence),
            access_restricted=any(_truthy(doc.get(key)) for key in _RESTRICTED_KEYS),
            borrow_only=borrow_only,
            # The decisive flag: outside a curated collection, IA metadata
            # is an uploader's claim, and the rights engine quarantines it.
            uploader_asserted_only=not in_curated_collection,
            raw_rights_text=rights_text or license_url,
        ),
        raw_metadata={
            "identifier": identifier,
            "collections": collections,
            "licenseurl": license_url,
            "rights": rights_text,
            "in_curated_collection": in_curated_collection,
        },
    )


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def choose_best_file(files: list[dict]) -> tuple[str, str, int]:
    """
    Pick one text derivative from an IA item's file list.

    Returns (name, format, size). Preference follows the corpus-wide
    format order, and files that are obviously not the work (thumbnails,
    metadata sidecars, per-page images, OCR intermediates) are excluded
    rather than ranked low -- ranking them low still lets one win when a
    real text is missing.
    """
    best: tuple[int, int, str, str] | None = None

    for entry in files:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        ia_format = str(entry.get("format") or "").strip().lower()
        if not name:
            continue
        lowered = name.lower()
        if lowered.endswith((".gif", ".jpg", ".jpeg", ".png", ".zip", ".gz", ".sqlite", ".xml.gz")):
            continue
        if "_meta" in lowered or "_files" in lowered or lowered.endswith("_scandata.xml"):
            continue

        fmt = _FORMAT_BY_IA_NAME.get(ia_format, "")
        if not fmt:
            for suffix, mapped in ((".txt", "txt"), (".epub", "epub"), (".pdf", "pdf"), (".html", "html")):
                if lowered.endswith(suffix):
                    fmt = mapped
                    break
        if not fmt:
            continue

        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0

        rank = rank_format(fmt)
        # Within a format, prefer the larger file: IA often carries both a
        # truncated sample and the full text.
        key = (rank, -size, name, fmt)
        if best is None or key < best:
            best = key

    if best is None:
        return "", "", 0
    return best[2], best[3], -best[1]


INTERNET_ARCHIVE_CAPABILITIES = SourceCapabilities(
    provider_id="internet_archive",
    #: Discovery and hosting are excellent. RIGHTS_EVIDENCE is
    #: deliberately absent from the unconditional set: `licenseurl` is
    #: what an uploader typed, and an uploader is not an authority on
    #: whether a work is in the public domain.
    capabilities=frozenset(
        {Capability.DISCOVERY, Capability.METADATA, Capability.CONTENT_HOSTING}
    ),
    rights_trust=0.35,
    content_hosts=("archive.org", "ia800000.us.archive.org"),
    notes=(
        "Superb catalogue, unreliable rights. `licenseurl` reflects the UPLOADER's "
        "assertion, not a determination by a rights holder or an institution, so it "
        "is not accepted on its own. A curated `trusted_collections` allowlist is the "
        "one narrow exception: inside an allowlisted institutional collection the "
        "rights statement has an identifiable institution behind it. Empty by "
        "default, which quarantines everything -- the safe direction."
    ),
)


@register_adapter
class InternetArchiveAdapter(SourceAdapter):
    """
    Options:
      `query`                -- IA advanced-search query
      `rows`                 -- results per page (default 50)
      `trusted_collections`  -- institutional collections whose rights
                                statements are trusted. Empty by default,
                                which means every item is quarantined.
                                `allowed_collections` is accepted as the
                                Phase I spelling.
    """

    adapter_name = "internet_archive"
    capabilities = INTERNET_ARCHIVE_CAPABILITIES

    def trusted_collections(self) -> list[str]:
        """
        The curated allowlist, under either spelling.

        Phase I called this `allowed_collections`. Renaming it outright
        would silently empty the allowlist of any deployment still using
        the old key -- and an empty allowlist quarantines everything,
        which is safe but looks exactly like the harvester breaking. Both
        are read, and both are honoured.
        """
        collections = (
            self.config.option("trusted_collections", None)
            or self.config.option("allowed_collections", None)
            or []
        )
        return [str(c) for c in collections]

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        page = int(cursor) if cursor and cursor.isdigit() else 1
        rows = int(self.config.option("rows", 50))
        query = self.config.option("query", "mediatype:texts AND licenseurl:*creativecommons*")

        url = build_search_url(self.config.base_url, query, rows, page)
        response = self.fetch_metadata(url, conditional=False)
        candidates, total, returned = parse_search_response(
            response.content, self.config.source_id, self.trusted_collections()
        )

        exhausted = returned < rows or page * rows >= total
        return DiscoveryResult(
            candidates=candidates,
            next_cursor=None if exhausted else str(page + 1),
            exhausted=exhausted,
        )

    def fetch_content(self, candidate: Candidate, max_bytes: int = 0):
        """
        Resolve the file list, choose the best text derivative, download
        only that one.
        """
        metadata_url = build_metadata_url(self.config.base_url, candidate.external_id)
        response = self.fetch_metadata(metadata_url, conditional=False)
        data = json.loads(response.content.decode("utf-8", errors="replace"))

        files = data.get("files", []) or []
        name, fmt, _size = choose_best_file(files)
        if not name:
            raise ValueError(f"Internet Archive item {candidate.external_id!r} has no usable text derivative")

        server = data.get("server") or "archive.org"
        directory = data.get("dir") or f"/{candidate.external_id}"
        download_url = f"https://{server}{directory}/{quote(name)}"

        return self.fetcher.fetch(
            download_url,
            allowed_hosts=self.config.allowed_hosts,
            max_bytes=max_bytes or self.config.max_download_bytes,
            allow_http=self.config.allow_http,
        )


__all__ = [
    "INTERNET_ARCHIVE_CAPABILITIES",
    "BORROW_ONLY_COLLECTIONS",
    "IA_UPLOADER_NOTE",
    "InternetArchiveAdapter",
    "build_metadata_url",
    "build_search_url",
    "choose_best_file",
    "parse_search_response",
]
