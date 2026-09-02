"""
Standard Ebooks, via its official OPDS feed.

Standard Ebooks produces carefully typeset editions of public-domain
works. Its rights position has **two distinct layers**, and conflating
them is precisely the error this project exists to avoid:

* **The underlying work** is, in Standard Ebooks' own words, "Public
  domain in the United States. Users located outside of the United
  States must check their local laws before using this ebook."
* **Standard Ebooks' own editorial contribution** -- the typesetting,
  proofreading, and semantic markup -- is released under CC0.

CC0 on the edition does **not** make the work worldwide public domain.
So this adapter takes the entry's own `<rights>` text as authoritative
(which normalizes to `public-domain-us`, scope `LOCAL_US_ONLY`) and
records the CC0 dedication as evidence *about the edition*, never as the
content licence. Under `local_research_us` that is an accept marked
LOCAL_US_ONLY; under `release_worldwide` it is a quarantine, and the
export guard will refuse to package it.

An earlier version of this adapter forced `provider_declared_scope =
"WORLDWIDE"` and overrode the licence to CC0. That was wrong, and it was
wrong in the single most consequential direction available.

The HTML site is never scraped. Two official feeds carry everything
needed: the OPDS catalogue (which now requires authentication, so it is
used only where credentials are configured -- never bypassed) and the
public `feeds/atom/new-releases` syndication feed.
"""

from __future__ import annotations

import logging

from ..models import Candidate, RightsSignal
from ..rights import evidence_from_field
from .base import DiscoveryResult, register_adapter
from .opds import OPDSAdapter, parse_opds_feed

logger = logging.getLogger(__name__)

#: Where Standard Ebooks publishes its dedication. Configurable, but this
#: default is the URL the evidence points at.
DEFAULT_LICENSE_URL = "https://standardebooks.org/licensing"

#: Describes the EDITION only. Recorded as evidence, never used as the
#: content licence -- see the module docstring.
CC0_STATEMENT = (
    "Standard Ebooks releases its own editorial contribution to this edition (typesetting, "
    "proofreading, semantic markup) to the public domain via CC0 1.0 Universal. This "
    "dedication covers the EDITION only; the rights status of the underlying work is stated "
    "separately in the entry's own rights element and is, in general, public domain in the "
    "United States rather than worldwide."
)


@register_adapter
class StandardEbooksAdapter(OPDSAdapter):
    """
    Options:
      `start_path`  -- OPDS entry point (default "/feeds/opds/all")
      `license_url` -- where the dedication is published
    """

    adapter_name = "standard_ebooks"

    def start_url(self) -> str:
        return super().start_url() if self.config.option("start_path") else self.config.base_url

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        url = cursor or self.start_url()
        response = self.fetch_metadata(url, accept="application/atom+xml")
        candidates, next_url = parse_opds_feed(response.content, url, self.config.source_id)
        enriched = [self._attach_dedication(c) for c in candidates]
        return DiscoveryResult(candidates=enriched, next_cursor=next_url, exhausted=next_url is None)

    def _attach_dedication(self, candidate: Candidate) -> Candidate:
        """
        Record the CC0 edition dedication as evidence, leaving the
        entry's own rights statement as the authoritative content licence.

        The entry says what the WORK's status is; the dedication says what
        Standard Ebooks did with its own contribution. Both are recorded,
        and only the former decides the licence. If Standard Ebooks ever
        changed its terms for some subset of entries, that per-entry
        statement would still be what governs -- which would not be true
        if this overwrote everything with a constant.
        """
        signal = candidate.rights
        license_url = self.config.option("license_url", DEFAULT_LICENSE_URL)

        evidence = list(signal.evidence)
        evidence.append(
            evidence_from_field("standard_ebooks:cc0-edition-dedication", CC0_STATEMENT, license_url)
        )

        # Only when the entry stated nothing at all does the dedication
        # stand in -- and then only as the edition's CC0, which the
        # rights engine will scope on its own merits.
        content_license = signal.content_license or "CC0 1.0 Universal (Standard Ebooks edition)"

        return Candidate(
            source_id=candidate.source_id,
            external_id=candidate.external_id,
            title=candidate.title,
            authors=candidate.authors,
            contributors=candidate.contributors,
            translator=candidate.translator,
            language=candidate.language or "en",
            publication_date=candidate.publication_date,
            edition_date=candidate.edition_date,
            document_type=candidate.document_type,
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
                # Deliberately NOT defaulted to the CC0 URI: that URI
                # describes the edition, and putting it here would let it
                # be read as the work's licence.
                rights_statement_uri=signal.rights_statement_uri,
                # Deliberately empty. The licence's own inherent scope
                # decides -- "public domain in the United States" is
                # LOCAL_US_ONLY, and no provider assertion may widen it.
                provider_declared_scope="",
                jurisdictions=signal.jurisdictions,
                evidence=tuple(evidence),
                raw_rights_text=signal.raw_rights_text,
            ),
            raw_metadata={**candidate.raw_metadata, "producer": "Standard Ebooks"},
        )


__all__ = ["CC0_STATEMENT", "DEFAULT_LICENSE_URL", "StandardEbooksAdapter"]
