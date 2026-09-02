"""
OAPEN and DOAB: open-access scholarly books, via their OAI-PMH endpoints.

These are among the cleanest sources available -- the whole point of both
platforms is that the *books* carry explicit open licences, not merely
the metadata. That does not exempt them from the metadata/content split,
which is why this adapter subclasses the generic OAI-PMH harvester and
then adds one specific behaviour: it looks for the licence attached to
the full-text bitstream rather than accepting a repository-level
statement.

A DOAB record can legitimately have:

* `dc:rights` describing the metadata's own licence, and
* a separate per-bitstream licence for the book itself.

Where both are present the book's licence wins and the metadata licence
is recorded as `metadata_license` so the distinction survives into the
rights decision. Where only a repository-level statement exists, it is
recorded as the content licence only if it names a specific licence --
a repository's general open-access policy statement is not a per-book
grant.

A record with no official download link is discovered but never becomes
an item: there is nothing to harvest, and constructing a download URL by
guessing the repository's file layout is exactly the kind of unofficial
access this project does not do.
"""

from __future__ import annotations

import logging
import re

from ..models import Candidate, RightsSignal
from ..rights import evidence_from_field
from .base import DiscoveryResult, register_adapter
from .oai_pmh import OAIPMHAdapter, build_list_records_url, build_resume_url, parse_oai_response

logger = logging.getLogger(__name__)

#: Licence URIs that appear in DOAB/OAPEN records and name a specific
#: grant, as opposed to describing an access model.
_SPECIFIC_LICENSE_RE = re.compile(
    r"creativecommons\.org/(?:licenses|publicdomain)/|rightsstatements\.org/vocab/", re.IGNORECASE
)

#: Phrases that describe access, not a licence. "Open access" is a
#: business model; it grants nothing on its own.
_ACCESS_MODEL_PHRASES = (
    "open access", "libre accès", "freier zugang", "acceso abierto",
    "accesso aperto", "free to read", "gold open access",
)


def refine_rights(candidate: Candidate, source_id: str) -> Candidate:
    """
    Separate a specific licence grant from an access-model statement.

    The rule applied here: a rights value that resolves to a Creative
    Commons or rightsstatements.org URI is a content licence. A value
    that only says "open access" is not, and is demoted to
    `raw_rights_text` so the rights engine sees an unknown licence and
    quarantines rather than accepting a business model as permission.
    """
    signal = candidate.rights
    all_values = [v for v in (candidate.raw_metadata.get("rights") or []) if isinstance(v, str)]

    specific: list[str] = []
    access_only: list[str] = []
    for value in all_values:
        if _SPECIFIC_LICENSE_RE.search(value):
            specific.append(value)
        elif any(phrase in value.lower() for phrase in _ACCESS_MODEL_PHRASES):
            access_only.append(value)
        else:
            specific.append(value) if len(value) > 12 else access_only.append(value)

    content_license = ""
    rights_uri = signal.rights_statement_uri
    for value in specific:
        if value.lower().startswith(("http://", "https://")):
            rights_uri = rights_uri or value
            content_license = content_license or value
        else:
            content_license = content_license or value

    evidence = list(signal.evidence)
    if access_only:
        evidence.append(
            evidence_from_field(
                "oapen:access-model-note",
                "Record carries an open-access statement: "
                + "; ".join(access_only)[:400]
                + " -- an access model, not a licence grant.",
                candidate.canonical_url,
            )
        )

    return Candidate(
        source_id=candidate.source_id,
        external_id=candidate.external_id,
        title=candidate.title,
        authors=candidate.authors,
        contributors=candidate.contributors,
        translator=candidate.translator,
        language=candidate.language,
        publication_date=candidate.publication_date,
        edition_date=candidate.edition_date,
        document_type=candidate.document_type or "book",
        subjects=candidate.subjects,
        canonical_url=candidate.canonical_url,
        download_url=candidate.download_url,
        download_format=candidate.download_format,
        estimated_bytes=candidate.estimated_bytes,
        work_identifiers=candidate.work_identifiers,
        alternate_downloads=candidate.alternate_downloads,
        rights=RightsSignal(
            content_license=content_license,
            metadata_license=signal.metadata_license,
            rights_statement_uri=rights_uri,
            provider_declared_scope=signal.provider_declared_scope,
            jurisdictions=signal.jurisdictions,
            evidence=tuple(evidence),
            # No official download link means nothing to harvest. Recording
            # it as access-restricted keeps it out of the queue without
            # pretending we evaluated a licence we never saw applied.
            access_restricted=not candidate.download_url,
            raw_rights_text=" | ".join(all_values)[:2000],
        ),
        raw_metadata=candidate.raw_metadata,
    )


@register_adapter
class OapenDoabAdapter(OAIPMHAdapter):
    """
    Options: inherits every OAI-PMH option (`metadata_prefix`, `set`,
    `from`).
    """

    adapter_name = "oapen_doab"

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
        refined = [refine_rights(c, self.config.source_id) for c in candidates]
        return DiscoveryResult(candidates=refined, next_cursor=token, exhausted=token is None)


__all__ = ["OapenDoabAdapter", "refine_rights"]
