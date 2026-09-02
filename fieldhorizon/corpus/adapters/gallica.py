"""
Gallica / BnF, via the official SRU search interface.

Gallica is the source where the metadata/scan/OCR/work rights split
matters most, and it is modelled explicitly:

* **the work** (the intellectual creation) may be in the public domain;
* **the scan** (BnF's digitisation) has its own reuse terms;
* **the OCR text** may be offered under different terms again;
* **the metadata** is separately licensed.

Gallica's `dc:rights` field distinguishes "domaine public" from
"Consultable en ligne" and similar restricted statements. Only the former
is a content-rights statement; the latter says you may *look at* the
object on Gallica, which is not a licence to harvest it. That distinction
is enforced here: a record without an affirmative public-domain statement
is marked `access_restricted`, which the rights engine rejects outright.

The existing text mode is always preferred over any local OCR (which is
not implemented at all). Where Gallica offers `.texteBrut`, that is the
download; where it does not, the record is still discovered but has no
usable download and never becomes an item.
"""

from __future__ import annotations

import logging
import re

from ..language import normalize_language_code
from ..models import Candidate, Evidence, RightsSignal
from ..rights import evidence_from_field
from ..security import local_name
from .base import DiscoveryResult, SourceAdapter, register_adapter
from .sru import build_sru_url, compute_next_start, parse_sru_envelope

logger = logging.getLogger(__name__)

#: Affirmative public-domain statements Gallica uses. Anything else is
#: not treated as a content-rights grant.
_PUBLIC_DOMAIN_MARKERS = (
    "domaine public",
    "public domain",
)

#: Statements that describe *access*, not reuse rights. Present on a
#: large share of Gallica records and routinely mistaken for permission.
_ACCESS_ONLY_MARKERS = (
    "consultable en ligne",
    "consultation en ligne",
    "acc[èe]s r[ée]serv[ée]",
    "communication diff[ée]r[ée]e",
    "droits r[ée]serv[ée]s",
    "sous droits",
)

_ACCESS_ONLY_RE = re.compile("|".join(_ACCESS_ONLY_MARKERS), re.IGNORECASE)

GALLICA_REUSE_URL = "https://gallica.bnf.fr/edit/und/conditions-dutilisation-des-contenus-de-gallica"

GALLICA_STATEMENT = (
    "Gallica distinguishes the rights of the digitised object from those of the work. "
    "Only records carrying an affirmative public-domain statement for the object are "
    "treated as reusable; access-only statements are not licences."
)


def _dc_text(record, name: str) -> list[str]:
    out: list[str] = []
    for element in record.iter():
        if local_name(element.tag).lower() == name.lower() and element.text:
            value = " ".join(element.text.split())
            if value:
                out.append(value)
    return out


def ark_from_identifier(value: str) -> str:
    match = re.search(r"(ark:/\d+/[a-z0-9]+)", value or "", re.IGNORECASE)
    return match.group(1) if match else ""


def parse_gallica_record(record_data, source_id: str) -> Candidate | None:
    """
    One SRU `recordData` element into a Candidate.

    Records with no ARK are skipped: the ARK is Gallica's stable
    identifier and without it there is no idempotent key and no way to
    construct a download URL.
    """
    identifiers = _dc_text(record_data, "identifier")
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

    canonical_url = canonical_url or f"https://gallica.bnf.fr/{ark}"

    titles = _dc_text(record_data, "title")
    creators = _dc_text(record_data, "creator")
    contributors = _dc_text(record_data, "contributor")
    subjects = _dc_text(record_data, "subject")
    languages = _dc_text(record_data, "language")
    dates = _dc_text(record_data, "date")
    types = _dc_text(record_data, "type")
    rights_values = _dc_text(record_data, "rights")
    formats = _dc_text(record_data, "format")

    rights_blob = " | ".join(rights_values)
    lowered = rights_blob.lower()

    is_public_domain = any(marker in lowered for marker in _PUBLIC_DOMAIN_MARKERS)
    access_only = bool(_ACCESS_ONLY_RE.search(rights_blob)) and not is_public_domain

    evidence: list[Evidence] = [
        evidence_from_field("gallica:dc:rights", rights_blob or "(no rights field)", canonical_url),
        evidence_from_field("gallica:reuse-conditions", GALLICA_STATEMENT, GALLICA_REUSE_URL),
    ]

    # Gallica's text mode. Preferred over any OCR we could run; when the
    # object has no text mode there is simply no download.
    text_url = f"https://gallica.bnf.fr/{ark}.texteBrut"

    return Candidate(
        source_id=source_id,
        external_id=ark,
        title=titles[0] if titles else "",
        authors=tuple(creators),
        contributors=tuple(contributors),
        language=normalize_language_code(languages[0]) if languages else "fr",
        publication_date=dates[0] if dates else "",
        document_type=types[0] if types else "",
        subjects=tuple(subjects[:24]),
        canonical_url=canonical_url,
        download_url=text_url,
        download_format="html",
        work_identifiers=(f"ark:{ark}",),
        rights=RightsSignal(
            # Only an affirmative public-domain statement counts as a
            # content licence; everything else is left empty so the rights
            # engine sees "no licence" rather than a misread access note.
            content_license=rights_blob if is_public_domain else "",
            provider_declared_scope="",
            jurisdictions=("FR",),
            evidence=tuple(evidence),
            access_restricted=access_only,
            raw_rights_text=rights_blob,
        ),
        raw_metadata={
            "ark": ark,
            "titles": titles,
            "creators": creators,
            "subjects": subjects[:24],
            "rights": rights_values,
            "formats": formats,
            "types": types,
        },
    )


@register_adapter
class GallicaAdapter(SourceAdapter):
    """
    Options:
      `query`      -- CQL query (default: French-language public-domain
                      monographs in text mode)
      `page_size`  -- records per SRU page (default 50)
    """

    adapter_name = "gallica"

    DEFAULT_QUERY = (
        'gallica all "domaine public" and dc.type all "monographie" and dc.language all "fre"'
    )

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        start = int(cursor) if cursor and cursor.isdigit() else 1
        page_size = int(self.config.option("page_size", 50))
        query = self.config.option("query", self.DEFAULT_QUERY)

        url = build_sru_url(self.config.base_url, query, start, page_size)
        response = self.fetch_metadata(url, accept="application/xml")
        records, total, reported_next = parse_sru_envelope(response.content)

        candidates: list[Candidate] = []
        for record in records:
            candidate = parse_gallica_record(record, self.config.source_id)
            if candidate is not None:
                candidates.append(candidate)

        next_start = compute_next_start(start, page_size, len(records), total, reported_next)
        return DiscoveryResult(
            candidates=candidates,
            next_cursor=str(next_start) if next_start else None,
            exhausted=next_start == 0,
        )


__all__ = ["GALLICA_STATEMENT", "GallicaAdapter", "ark_from_identifier", "parse_gallica_record"]
