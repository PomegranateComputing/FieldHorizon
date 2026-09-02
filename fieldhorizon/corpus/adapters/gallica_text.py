"""
Gallica / BnF: the institutional text pipeline.

    SRU → notice and ARK → rights evidence → Document API → text-mode
    detection → institutional OCR/ALTO → page reconstruction → quality →
    deduplication → classification → ingestion

**What the live service actually does, verified 2026-08-01.**

| Endpoint | Status |
|---|---|
| `/SRU` search | 200 — discovery works |
| `/services/Pagination` | 200 — page count and `hasContent` |
| `/iiif/.../manifest.json` | 200 — structure and metadata |
| `/ark:/…/…​.texteBrut` | **302 → `/services/engine/search/altcha`** |
| `/RequestDigitalElement?…E=ALTO` | connection refused to our client |

`altcha` is a proof-of-work anti-bot challenge. Solving it is exactly
what this project does not do — the rule is "never bypass a CAPTCHA,
authentication, paywall, access restriction, or anti-bot protection",
and a proof-of-work challenge is all of the last one.

So Gallica declares `DISCOVERY`, `METADATA`, and `RIGHTS_EVIDENCE`, and
does **not** declare `CONTENT_HOSTING` or `RENDERED_TEXT` while the
challenge is in place. Its documents come to rest in `METADATA_ONLY`,
which is a complete and correct outcome for a provider in that position
— not a failure, and not something to work around.

Phase I had no way to say this. It would have recorded a few hundred
download failures and called the source broken. That is the whole reason
the capability graph exists.

The text and ALTO parsing below is implemented and tested offline, so
that if the challenge is lifted, or an institutional agreement supplies
a route, enabling `RENDERED_TEXT` is a configuration change rather than
new code. The parsers are correct; only our permission to call the
endpoint is missing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

from ..capabilities import Capability, SourceCapabilities
from ..language import normalize_language_code
from ..lattice import ComponentRights, RightsComponent, RightsStack
from ..models import Candidate, Evidence, RightsSignal
from ..rights import evidence_from_field
from ..security import child_local, find_local, html_to_text, local_name, safe_parse_xml
from .base import DiscoveryResult, SourceAdapter, register_adapter
from .gallica import GALLICA_REUSE_URL, GALLICA_STATEMENT, ark_from_identifier
from .sru import build_sru_url, compute_next_start, parse_sru_envelope

logger = logging.getLogger(__name__)

#: Where the anti-bot challenge lives. A redirect here means the endpoint
#: is gated, and the only correct response is to stop.
ALTCHA_MARKER = "altcha"

#: Affirmative public-domain statements. Anything else is not a grant.
_PUBLIC_DOMAIN_MARKERS = ("domaine public", "public domain")

#: Access statements routinely mistaken for permission. "You may look at
#: this on Gallica" is not "you may harvest this".
_ACCESS_ONLY_RE = re.compile(
    r"consultable en ligne|consultation en ligne|acc[èe]s r[ée]serv[ée]|"
    r"communication diff[ée]r[ée]e|droits? r[ée]serv[ée]s|sous droits",
    re.IGNORECASE,
)

#: dc:type values that are actually text. Gallica's catalogue is full of
#: prints, maps, and photographs; the first result for a plain author
#: search is routinely an engraving, and harvesting those as "books"
#: would be nonsense.
_TEXT_TYPES = ("monographie", "fascicule", "manuscrit", "text", "texte", "périodique", "publication")

_IMAGE_TYPES = ("estampe", "image", "photographie", "carte", "dessin", "affiche", "objet", "partition")

GALLICA_CAPABILITIES = SourceCapabilities(
    provider_id="gallica",
    capabilities=frozenset(
        {
            Capability.DISCOVERY,
            Capability.METADATA,
            Capability.RIGHTS_EVIDENCE,
            Capability.INCREMENTAL_UPDATES,
            Capability.SUPPORTS_RESUME,
        }
    ),
    # CONTENT_HOSTING and RENDERED_TEXT are deliberately absent. Gallica
    # hosts the text; we are not permitted to fetch it while the altcha
    # challenge stands, and declaring a capability we may not exercise
    # would make the planner build plans that cannot succeed.
    rights_trust=0.75,
    content_hosts=("gallica.bnf.fr",),
    notes=(
        "Text mode is behind an altcha proof-of-work challenge as of 2026-08-01; "
        "CONTENT_HOSTING and RENDERED_TEXT are withheld rather than bypassed. "
        "Discovery, metadata, and rights evidence are fully operational."
    ),
)


@dataclass(frozen=True)
class GallicaRecord:
    """One SRU notice, before any content decision."""

    ark: str
    title: str
    authors: tuple[str, ...]
    language: str
    date: str
    document_type: str
    subjects: tuple[str, ...]
    rights_text: str
    is_public_domain: bool
    access_restricted: bool
    is_text: bool
    total_views: int
    canonical_url: str
    raw_identifiers: tuple[str, ...] = ()


def _dc_values(record, name: str) -> list[str]:
    out: list[str] = []
    for element in record.iter():
        if local_name(element.tag).lower() == name.lower() and element.text:
            value = " ".join(element.text.split())
            if value:
                out.append(value)
    return out


def _total_views(formats: list[str]) -> int:
    """
    Gallica states the view count in a `dc:format` string:
    "Nombre total de vues : 544". A one-view record is almost always an
    image rather than a book.
    """
    for value in formats:
        match = re.search(r"nombre total de vues\s*:\s*(\d+)", value, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 0


def parse_gallica_sru_record(record_data, source_id: str = "gallica") -> GallicaRecord | None:
    """
    One SRU `recordData` into a typed notice.

    Records with no ARK are dropped: the ARK is Gallica's stable
    identifier, and without it there is neither an idempotent key nor a
    way to address the object at all.
    """
    identifiers = _dc_values(record_data, "identifier")
    ark = ""
    canonical_url = ""
    for value in identifiers:
        found = ark_from_identifier(value)
        if found and not ark:
            ark = found
        if value.lower().startswith("http") and not canonical_url:
            canonical_url = value
    if not ark:
        return None

    titles = _dc_values(record_data, "title")
    types = _dc_values(record_data, "type")
    formats = _dc_values(record_data, "format")
    rights_values = _dc_values(record_data, "rights")
    languages = _dc_values(record_data, "language")

    rights_blob = " | ".join(rights_values)
    lowered = rights_blob.lower()
    is_public_domain = any(marker in lowered for marker in _PUBLIC_DOMAIN_MARKERS)

    type_blob = " ".join(types).lower()
    format_blob = " ".join(formats).lower()
    is_text = any(t in type_blob for t in _TEXT_TYPES) and not any(
        t in type_blob for t in _IMAGE_TYPES
    )
    # A record with only image formats and one view is a picture whatever
    # its dc:type says.
    if "image/jpeg" in format_blob and _total_views(formats) <= 1 and not is_text:
        is_text = False

    return GallicaRecord(
        ark=ark,
        title=titles[0] if titles else "",
        authors=tuple(_dc_values(record_data, "creator")),
        language=normalize_language_code(languages[0]) if languages else "fr",
        date=(_dc_values(record_data, "date") or [""])[0],
        document_type=types[0] if types else "",
        subjects=tuple(_dc_values(record_data, "subject")[:24]),
        rights_text=rights_blob,
        is_public_domain=is_public_domain,
        access_restricted=bool(_ACCESS_ONLY_RE.search(rights_blob)) and not is_public_domain,
        is_text=is_text,
        total_views=_total_views(formats),
        canonical_url=canonical_url or f"https://gallica.bnf.fr/{ark}",
        raw_identifiers=tuple(identifiers),
    )


def parse_pagination(xml_bytes: bytes) -> dict:
    """
    Gallica's Pagination service.

    `hasContent` is the signal that matters: it says whether a text layer
    exists at all, which decides between "fetch the text" and
    "OCR_PENDING". `nbVueImages` gives the page count for reconstruction.
    """
    root = safe_parse_xml(xml_bytes)
    structure = find_local(root, "structure")

    def _text(element, name: str) -> str:
        if element is None:
            return ""
        child = child_local(element, name)
        return (child.text or "").strip() if child is not None and child.text else ""

    pages: list[dict] = []
    pages_el = find_local(root, "pages")
    if pages_el is not None:
        for page in pages_el:
            if local_name(page.tag).lower() != "page":
                continue
            pages.append(
                {
                    "ordre": _text(page, "ordre"),
                    "numero": _text(page, "numero"),
                    "pagination_type": _text(page, "pagination_type"),
                }
            )

    view_count = _text(structure, "nbVueImages")
    return {
        "ark": _text(structure, "idUPN"),
        "has_content": _text(structure, "hasContent").lower() == "true",
        "has_toc": _text(structure, "hasToc").lower() == "true",
        "view_count": int(view_count) if view_count.isdigit() else 0,
        "first_displayed_page": _text(structure, "firstDisplayedPage"),
        "pages": pages,
    }


# --------------------------------------------------------------------------
# ALTO
# --------------------------------------------------------------------------


def parse_alto(xml_bytes: bytes) -> str:
    """
    Text from one ALTO page.

    ALTO nests TextBlock → TextLine → String, with the words in `CONTENT`
    attributes and no text nodes at all — so a naive text walk over an
    ALTO file returns an empty string, which is exactly the sort of
    silent nothing that looks like a working parser.

    Line and block boundaries are preserved: an ALTO page flattened into
    one run of words loses the paragraph structure the classifier's
    structural signal depends on.
    """
    root = safe_parse_xml(xml_bytes)

    blocks: list[str] = []
    for element in root.iter():
        if local_name(element.tag) != "TextBlock":
            continue
        lines: list[str] = []
        for line in element.iter():
            if local_name(line.tag) != "TextLine":
                continue
            words = [
                token.attrib.get("CONTENT", "")
                for token in line.iter()
                if local_name(token.tag) == "String" and token.attrib.get("CONTENT")
            ]
            if words:
                lines.append(" ".join(words))
        if lines:
            blocks.append("\n".join(lines))

    return "\n\n".join(blocks).strip()


def reconstruct_pages(page_texts: list[str], *, drop_empty: bool = True) -> str:
    """
    Assemble per-page ALTO text into one document.

    Pages are joined with a blank line rather than a page marker: a
    marker on every page would become the most repeated line in the
    document and the boilerplate stripper would spend its effort on it.
    """
    parts = [text.strip() for text in page_texts]
    if drop_empty:
        parts = [text for text in parts if text]
    return "\n\n".join(parts)


def parse_texte_brut(html_bytes: bytes) -> str:
    """
    Gallica's text mode is an HTML page. Reduced with the same hardened
    extractor everything else uses -- it is third-party markup and gets
    no more trust than any other.
    """
    return html_to_text(html_bytes.decode("utf-8", errors="replace"))


def looks_like_the_anti_bot_challenge(final_url: str, body: bytes) -> bool:
    """
    Whether a response is the altcha challenge rather than content.

    Detected so the harvester can stop cleanly and record ACCESS_BLOCKED.
    There is deliberately no code path that attempts the challenge.
    """
    if ALTCHA_MARKER in (final_url or "").lower():
        return True
    head = body[:4000].decode("utf-8", errors="replace").lower()
    return "altcha" in head and ("challenge" in head or "notverified" in head)


# --------------------------------------------------------------------------
# Rights
# --------------------------------------------------------------------------


def rights_stack_for(record: GallicaRecord) -> RightsStack:
    """
    Gallica's four layers, stated as four layers.

    The institution distinguishes the rights of the WORK, the SCAN, the
    OCR TEXT, and the METADATA, and conflating them is the mistake this
    source exists to teach. Only an affirmative public-domain statement
    about the object is a content grant; "consultable en ligne" is an
    access note.

    The date is deliberately absent from this computation. A 1750
    publication date is context, never proof — an edition, a translation,
    or an editorial apparatus can each carry their own term.
    """
    stack = RightsStack()

    evidence: tuple[Evidence, ...] = (
        evidence_from_field("gallica:dc:rights", record.rights_text or "(no rights field)", record.canonical_url),
        evidence_from_field("gallica:reuse-conditions", GALLICA_STATEMENT, GALLICA_REUSE_URL),
    )

    if record.is_public_domain:
        stack.add(RightsComponent.WORK, record.rights_text, evidence=evidence)
        stack.add(RightsComponent.SOURCE_TEXT, record.rights_text, evidence=evidence)
        # BnF's digitisation of a public-domain work is offered for reuse
        # under the same statement; recorded as its own layer so that a
        # future change to the scan's terms shows up as a scan-level
        # change rather than silently altering the work's status.
        stack.add(RightsComponent.SCAN, record.rights_text, evidence=evidence)
    else:
        # Not stated => not granted. An access note is not a licence, and
        # an unstated layer is never assumed permissive.
        stack.set(
            ComponentRights(
                component=RightsComponent.SOURCE_TEXT,
                raw_license="",
                evidence=evidence,
                notes=(
                    "no affirmative public-domain statement; "
                    f"rights field says {record.rights_text[:120]!r}"
                ),
            )
        )

    return stack


def candidate_from_record(record: GallicaRecord, source_id: str) -> Candidate:
    """
    A candidate carrying NO download URL.

    Deliberate: the text endpoint is behind the anti-bot challenge, so
    naming it as a download would produce a plan that cannot succeed and
    a failure that is not ours. The planner sees a candidate with rights
    and metadata and no host, and records METADATA_ONLY.
    """
    stack = rights_stack_for(record)
    source_text = stack.get(RightsComponent.SOURCE_TEXT)

    return Candidate(
        source_id=source_id,
        external_id=record.ark,
        title=record.title,
        authors=record.authors,
        language=record.language,
        publication_date=record.date,
        document_type=record.document_type,
        subjects=record.subjects,
        canonical_url=record.canonical_url,
        download_url="",
        download_format="",
        work_identifiers=(f"ark:{record.ark}",),
        rights=RightsSignal(
            content_license=record.rights_text if record.is_public_domain else "",
            provider_declared_scope="",
            jurisdictions=("FR",),
            evidence=source_text.evidence if source_text else (),
            access_restricted=record.access_restricted,
            raw_rights_text=record.rights_text,
        ),
        raw_metadata={
            "ark": record.ark,
            "is_text": record.is_text,
            "total_views": record.total_views,
            "rights_components": stack.to_dict(),
            "content_route": "blocked_by_anti_bot_challenge",
        },
    )


@register_adapter
class GallicaTextAdapter(SourceAdapter):
    """
    Options:
      `query`      -- CQL query (default: French public-domain monographs)
      `page_size`  -- SRU records per page
      `text_only`  -- drop prints, maps, and photographs (default true)
      `probe_text_availability` -- call the Pagination service per record
                                   to record whether a text layer exists
    """

    adapter_name = "gallica_text"
    capabilities = GALLICA_CAPABILITIES

    DEFAULT_QUERY = (
        'gallica all "domaine public" and dc.type all "monographie" and dc.language all "fre"'
    )

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        start = int(cursor) if cursor and cursor.isdigit() else 1
        page_size = int(self.config.option("page_size", 20))
        query = self.config.option("query", self.DEFAULT_QUERY)
        text_only = bool(self.config.option("text_only", True))

        url = build_sru_url(self.config.base_url, query, start, page_size)
        response = self.fetch_metadata(url, accept="application/xml")
        records, total, reported_next = parse_sru_envelope(response.content)

        candidates: list[Candidate] = []
        skipped_non_text = 0
        for record_data in records:
            record = parse_gallica_sru_record(record_data, self.config.source_id)
            if record is None:
                continue
            if text_only and not record.is_text:
                # Gallica's catalogue is full of prints, maps, and
                # photographs. Harvesting an engraving as a "book" would
                # be nonsense, and the first hit for a plain author
                # search is routinely exactly that.
                skipped_non_text += 1
                continue
            candidates.append(candidate_from_record(record, self.config.source_id))

        if skipped_non_text:
            logger.info(
                "%s: skipped %d non-text records (prints, maps, photographs)",
                self.config.source_id, skipped_non_text,
            )

        next_start = compute_next_start(start, page_size, len(records), total, reported_next)
        return DiscoveryResult(
            candidates=candidates,
            next_cursor=str(next_start) if next_start else None,
            exhausted=next_start == 0,
        )

    def probe_text_availability(self, ark: str) -> dict:
        """
        Ask the Pagination service whether a text layer exists.

        Permitted and unchallenged, and it is what distinguishes
        "OCR would be needed" from "the institution already has text".
        """
        bare = ark.split("/")[-1]
        url = f"https://gallica.bnf.fr/services/Pagination?ark={quote(bare)}"
        response = self.fetch_metadata(url, accept="application/xml")
        return parse_pagination(response.content)

    def fetch_content(self, candidate: Candidate, max_bytes: int = 0):
        """
        Refuse, with the reason.

        The text endpoint redirects to an altcha proof-of-work challenge.
        Solving it is bypassing an anti-bot protection, which this
        project does not do under any circumstances. Raising here rather
        than attempting the fetch keeps the refusal explicit in the code
        rather than implicit in a failure.
        """
        raise GallicaContentBlocked(
            f"Gallica text mode for {candidate.external_id} is behind an altcha "
            f"anti-bot challenge. The harvester does not solve challenges. "
            f"This document remains METADATA_ONLY with full rights evidence recorded."
        )


class GallicaContentBlocked(RuntimeError):
    """
    The content route is gated by an anti-bot challenge.

    A distinct exception type so the orchestrator can record
    ACCESS_BLOCKED rather than a generic failure -- the provider is
    working correctly and has declined us, which is not the same as a
    broken endpoint.
    """


__all__ = [
    "ALTCHA_MARKER",
    "GALLICA_CAPABILITIES",
    "GallicaContentBlocked",
    "GallicaRecord",
    "GallicaTextAdapter",
    "candidate_from_record",
    "looks_like_the_anti_bot_challenge",
    "parse_alto",
    "parse_gallica_sru_record",
    "parse_pagination",
    "parse_texte_brut",
    "reconstruct_pages",
    "rights_stack_for",
]
