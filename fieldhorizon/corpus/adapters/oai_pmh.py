"""
OAI-PMH 2.0 harvesting -- the second reusable brick.

OAI-PMH is the lingua franca of institutional repositories, so this
adapter backs OAPEN/DOAB and can back Gallica and most university
repositories through configuration alone.

Three protocol details are load-bearing and easy to get wrong:

* **resumptionToken is the cursor, and it is exclusive of every other
  argument.** A `ListRecords` continuation sends `verb` and
  `resumptionToken` and nothing else. Sending `metadataPrefix` alongside
  it is the single most common OAI-PMH client bug and produces a
  `badArgument` error from every compliant repository.
* **An empty `resumptionToken` element means the harvest is complete.**
  Absent and empty are both terminal; only a non-empty token continues.
* **`status="deleted"` headers carry no metadata.** They are tombstones
  and are skipped.

Dublin Core `dc:rights` is a free-text field and routinely contains
either a licence URI, a prose statement, or a rights-statement URI. All
three are captured as evidence; none is adjudicated here.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from ..language import normalize_language_code
from ..models import Candidate, Evidence, RightsSignal
from ..normalization import rank_format
from ..rights import evidence_from_field
from ..security import child_local, children_local, find_local, local_name, safe_parse_xml
from .base import DiscoveryResult, SourceAdapter, register_adapter

logger = logging.getLogger(__name__)


class OAIPMHError(RuntimeError):
    """The repository returned an <error> element."""


def build_list_records_url(base_url: str, metadata_prefix: str, set_spec: str = "", from_date: str = "") -> str:
    params = {"verb": "ListRecords", "metadataPrefix": metadata_prefix}
    if set_spec:
        params["set"] = set_spec
    if from_date:
        params["from"] = from_date
    return f"{base_url}?{urlencode(params)}"


def build_resume_url(base_url: str, token: str) -> str:
    """
    Continuation URL: `verb` and `resumptionToken` ONLY. See the module
    docstring -- adding metadataPrefix here is a protocol error.
    """
    return f"{base_url}?{urlencode({'verb': 'ListRecords', 'resumptionToken': token})}"


def parse_oai_response(xml_bytes: bytes, source_id: str, base_url: str) -> tuple[list[Candidate], str | None]:
    """
    Parse an OAI-PMH ListRecords response.

    Returns (candidates, resumption_token). A `None` token means the
    harvest is complete -- which includes the case of a present but empty
    `<resumptionToken/>` element.
    """
    root = safe_parse_xml(xml_bytes)

    error = find_local(root, "error")
    if error is not None:
        code = error.attrib.get("code", "unknown")
        raise OAIPMHError(f"OAI-PMH error {code}: {(error.text or '').strip()}")

    list_records = find_local(root, "ListRecords")
    if list_records is None:
        return [], None

    candidates: list[Candidate] = []
    for record in children_local(list_records, "record"):
        header = child_local(record, "header")
        if header is not None and header.attrib.get("status") == "deleted":
            continue
        candidate = _parse_record(record, source_id, base_url)
        if candidate is not None:
            candidates.append(candidate)

    token: str | None = None
    token_el = child_local(list_records, "resumptionToken")
    if token_el is not None and token_el.text and token_el.text.strip():
        token = token_el.text.strip()

    return candidates, token


def _dc_values(metadata_el, name: str) -> list[str]:
    """All values of one Dublin Core element, namespace-agnostic."""
    out: list[str] = []
    for element in metadata_el.iter():
        if local_name(element.tag).lower() == name.lower() and element.text:
            value = " ".join(element.text.split())
            if value:
                out.append(value)
    return out


_FORMAT_HINTS = {
    "application/pdf": "pdf",
    "application/epub+zip": "epub",
    "text/plain": "txt",
    "text/html": "html",
    "application/xml": "xml",
}


def _parse_record(record, source_id: str, base_url: str) -> Candidate | None:
    header = child_local(record, "header")
    identifier = ""
    if header is not None:
        id_el = child_local(header, "identifier")
        if id_el is not None and id_el.text:
            identifier = id_el.text.strip()
    if not identifier:
        return None

    metadata = child_local(record, "metadata")
    if metadata is None:
        return None

    titles = _dc_values(metadata, "title")
    creators = _dc_values(metadata, "creator")
    contributors = _dc_values(metadata, "contributor")
    subjects = _dc_values(metadata, "subject")
    languages = _dc_values(metadata, "language")
    dates = _dc_values(metadata, "date")
    types = _dc_values(metadata, "type")
    formats = _dc_values(metadata, "format")
    rights_values = _dc_values(metadata, "rights")
    identifiers = _dc_values(metadata, "identifier")

    # dc:identifier is a grab-bag: URLs, DOIs, ISBNs, handles. URLs that
    # look like content downloads are ranked by format preference; the
    # first non-download URL becomes the canonical record link.
    downloads: list[tuple[str, str]] = []
    canonical_url = ""
    for value in identifiers:
        if not value.lower().startswith(("http://", "https://")):
            continue
        lowered = value.lower()
        fmt = ""
        for suffix, mapped in ((".pdf", "pdf"), (".epub", "epub"), (".txt", "txt"), (".xml", "xml")):
            if lowered.endswith(suffix) or f"{suffix}?" in lowered:
                fmt = mapped
                break
        if not fmt and any(marker in lowered for marker in ("/download", "/bitstream", "/fulltext")):
            # A download endpoint with no extension: infer from dc:format
            # when it says something usable, otherwise leave it unknown
            # and let MIME sniffing settle it after the fetch.
            for value_format in formats:
                fmt = _FORMAT_HINTS.get(value_format.split(";")[0].strip().lower(), "")
                if fmt:
                    break
            fmt = fmt or "unknown"
        if fmt:
            downloads.append((fmt, value))
        elif not canonical_url:
            canonical_url = value

    downloads.sort(key=lambda d: rank_format(d[0]))

    rights_text = ""
    rights_uri = ""
    evidence: list[Evidence] = []
    for value in rights_values:
        if value.lower().startswith(("http://", "https://")):
            if not rights_uri:
                rights_uri = value
        elif not rights_text:
            rights_text = value
        evidence.append(evidence_from_field("oai:dc:rights", value, canonical_url or base_url))

    # OpenAIRE's `oaire:licenseCondition` is where DSpace-based
    # repositories (DOAB and OAPEN among them) actually put the licence.
    # Their `dc:rights` carries an ACCESS statement instead -- typically
    # "open access" with a COAR access-right URI, which is a business
    # model and not a grant. Reading only dc:rights meant every DOAB
    # record looked unlicensed and was quarantined, while the real
    # licence sat one element away.
    for element in metadata.iter():
        if local_name(element.tag).lower() != "licensecondition":
            continue
        uri = ""
        for key, value in element.attrib.items():
            if local_name(key).lower() == "uri" and value:
                uri = value.strip()
                break
        label = " ".join((element.text or "").split())
        if uri:
            rights_uri = rights_uri or uri
            rights_text = rights_text or uri
            evidence.append(
                evidence_from_field("oai:oaire:licenseCondition", uri, canonical_url or base_url)
            )
        elif label:
            rights_text = rights_text or label
            evidence.append(
                evidence_from_field("oai:oaire:licenseCondition", label, canonical_url or base_url)
            )

    return Candidate(
        source_id=source_id,
        external_id=identifier,
        title=titles[0] if titles else "",
        authors=tuple(creators),
        contributors=tuple(contributors),
        language=normalize_language_code(languages[0]) if languages else "",
        publication_date=dates[0] if dates else "",
        document_type=types[0] if types else "",
        subjects=tuple(subjects[:24]),
        canonical_url=canonical_url,
        download_url=downloads[0][1] if downloads else "",
        download_format=downloads[0][0] if downloads else "",
        alternate_downloads=tuple(downloads[1:]),
        rights=RightsSignal(
            content_license=rights_text,
            rights_statement_uri=rights_uri,
            evidence=tuple(evidence),
            raw_rights_text=" | ".join(rights_values)[:2000],
        ),
        raw_metadata={
            "identifier": identifier,
            "titles": titles,
            "creators": creators,
            "subjects": subjects[:24],
            "types": types,
            "formats": formats,
            "rights": rights_values,
            "identifiers": identifiers,
        },
    )


@register_adapter
class OAIPMHAdapter(SourceAdapter):
    """
    Generic OAI-PMH ListRecords harvester.

    Options:
      `metadata_prefix` -- default "oai_dc"
      `set`             -- optional setSpec
      `from`            -- optional ISO date for incremental harvesting
    """

    adapter_name = "oai_pmh"

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        base = self.config.base_url
        if cursor:
            url = build_resume_url(base, cursor)
        else:
            url = build_list_records_url(
                base,
                self.config.option("metadata_prefix", "oai_dc"),
                self.config.option("set", ""),
                self.config.option("from", ""),
            )

        response = self.fetch_metadata(url, accept="application/xml")
        candidates, token = parse_oai_response(response.content, self.config.source_id, base)
        return DiscoveryResult(candidates=candidates, next_cursor=token, exhausted=token is None)


__all__ = ["OAIPMHAdapter", "OAIPMHError", "build_list_records_url", "build_resume_url", "parse_oai_response"]
