"""
Europeana: a discovery and rights-structuring surface, not a content
host.

Europeana aggregates metadata from thousands of institutions and
publishes **its metadata** under CC0. That fact is the single largest
source of rights confusion in open-corpus work, and this adapter is
built around refusing to make the mistake:

    Europeana's CC0 metadata licence says NOTHING about the digital
    object.

So `metadata_license` is always set to CC0 (truthfully), and
`content_license` is populated *only* from the per-item `rights` field,
which is a rightsstatements.org or creativecommons.org URI describing the
object itself. The rights engine treats "open metadata licence, no
content licence" as a quarantine with `METADATA_LICENSE_ONLY` -- the
whole reason that reason code exists.

Content is never fetched from Europeana. It is fetched from the
providing institution's own official URL (`edmIsShownBy`), and only when
that URL's host passes the source's allowlist, so an aggregator record
cannot direct the harvester at an arbitrary domain.

An API key is required and comes from the environment
(`EUROPEANA_API_KEY`), never from a config file, and is redacted from
logs by the fetcher.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlencode, urlparse

from ..capabilities import Capability, SourceCapabilities
from ..language import normalize_language_code
from ..models import Candidate, Evidence, RightsSignal
from ..rights import evidence_from_field
from ..security import host_allowed
from .base import DiscoveryResult, SourceAdapter, register_adapter

logger = logging.getLogger(__name__)

EUROPEANA_METADATA_LICENSE = "CC0 1.0 Universal (Europeana metadata only)"
EUROPEANA_METADATA_NOTE = (
    "Europeana publishes its aggregated METADATA under CC0. This is not a licence for the "
    "digital object, whose rights are stated per-item in the edm:rights field."
)

#: Rights URIs that describe an openly reusable object. Everything else
#: -- including every rightsstatements.org "In Copyright" variant -- is
#: passed through unchanged for the rights engine to refuse.
_OPEN_PREFIXES = (
    "http://creativecommons.org/publicdomain/mark/",
    "https://creativecommons.org/publicdomain/mark/",
    "http://creativecommons.org/publicdomain/zero/",
    "https://creativecommons.org/publicdomain/zero/",
    "http://creativecommons.org/licenses/by/",
    "https://creativecommons.org/licenses/by/",
    "http://creativecommons.org/licenses/by-sa/",
    "https://creativecommons.org/licenses/by-sa/",
)


def build_search_url(base_url: str, query: str, api_key: str, rows: int = 50, cursor: str = "*") -> str:
    """
    Europeana's cursor-based paging (`cursorMark`) is used rather than
    `start`, because deep paging with `start` is both slow and unstable
    across a moving index -- a record inserted mid-harvest shifts every
    subsequent offset.
    """
    params = {
        "wskey": api_key,
        "query": query,
        "rows": str(rows),
        "cursor": cursor,
        "profile": "rich",
        "media": "true",
    }
    return f"{base_url.rstrip('/')}/record/v2/search.json?{urlencode(params)}"


def _first(value, default: str = "") -> str:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
        return default
    if isinstance(value, str):
        return value.strip() or default
    if isinstance(value, dict):
        for key in ("def", "en", "fr", "de"):
            if key in value:
                return _first(value[key], default)
    return default


def _all_strings(value) -> list[str]:
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_all_strings(item))
        return out
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_all_strings(item))
        return out
    return []


def parse_search_response(payload: bytes, source_id: str, allowed_hosts: list[str]) -> tuple[list[Candidate], str]:
    """
    Returns (candidates, next_cursor). An empty cursor means exhausted.

    A record whose `edmIsShownBy` host is not allowlisted is skipped with
    a log line rather than followed: an aggregator that can point the
    harvester at any host on the internet is a domain-allowlist bypass,
    however well-intentioned the aggregator.
    """
    data = json.loads(payload.decode("utf-8", errors="replace"))
    if not data.get("success", True):
        raise RuntimeError(f"Europeana API error: {data.get('error', 'unspecified')}")

    next_cursor = str(data.get("nextCursor") or "")
    candidates: list[Candidate] = []

    for item in data.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        candidate = _parse_item(item, source_id, allowed_hosts)
        if candidate is not None:
            candidates.append(candidate)

    return candidates, next_cursor


def _parse_item(item: dict, source_id: str, allowed_hosts: list[str]) -> Candidate | None:
    europeana_id = str(item.get("id") or "").strip()
    if not europeana_id:
        return None

    title = _first(item.get("title") or item.get("dcTitleLangAware"))
    creators = _all_strings(item.get("dcCreator") or item.get("dcCreatorLangAware"))
    subjects = _all_strings(item.get("dcSubject") or item.get("dcSubjectLangAware"))[:24]
    languages = _all_strings(item.get("language"))
    dates = _all_strings(item.get("year") or item.get("dcDate"))
    types = _all_strings(item.get("dcType") or item.get("type"))

    rights_uris = _all_strings(item.get("rights") or item.get("edmRights"))
    rights_uri = rights_uris[0] if rights_uris else ""

    # The broker's central question: WHO would host the bytes, and are we
    # willing to talk to them? Europeana can name any host on the
    # internet, so an aggregator followed blindly IS an allowlist bypass.
    # Every rejected host is recorded rather than merely logged, because
    # "Europeana found 400 objects and we can fetch 12" is a fact an
    # operator needs in order to decide which institutions to allowlist
    # next -- and in Phase I it existed only in the log.
    content_url = ""
    content_host = ""
    rejected_hosts: list[str] = []
    for url in _all_strings(item.get("edmIsShownBy")):
        host = (urlparse(url).hostname or "").lower()
        if host and host_allowed(host, allowed_hosts):
            content_url = url
            content_host = host
            break
        if host:
            rejected_hosts.append(host)
        logger.info(
            "Europeana %s: provider content host %r is not allowlisted; not harvesting the object",
            europeana_id, host,
        )

    canonical_url = _first(item.get("edmIsShownAt")) or f"https://www.europeana.eu/item{europeana_id}"

    evidence: list[Evidence] = [
        evidence_from_field("europeana:metadata-license", EUROPEANA_METADATA_NOTE, "https://www.europeana.eu/rights"),
    ]
    if rights_uri:
        evidence.append(evidence_from_field("europeana:edm:rights", rights_uri, canonical_url))

    is_open = any(rights_uri.startswith(prefix) for prefix in _OPEN_PREFIXES)

    return Candidate(
        source_id=source_id,
        external_id=europeana_id,
        title=title,
        authors=tuple(creators),
        language=normalize_language_code(languages[0]) if languages else "",
        publication_date=dates[0] if dates else "",
        document_type=types[0] if types else "",
        subjects=tuple(subjects),
        canonical_url=canonical_url,
        download_url=content_url,
        download_format="",
        rights=RightsSignal(
            # Only the object's own rights URI. Never the metadata licence.
            content_license=rights_uri if is_open else "",
            metadata_license=EUROPEANA_METADATA_LICENSE,
            rights_statement_uri=rights_uri,
            evidence=tuple(evidence),
            # No reachable object URL is an access restriction in
            # practice, whatever the rights statement claims.
            access_restricted=not content_url,
            raw_rights_text=rights_uri,
        ),
        raw_metadata={
            "europeana_id": europeana_id,
            "title": title,
            "creators": creators,
            "subjects": subjects,
            "rights": rights_uris,
            "provider": _first(item.get("dataProvider")),
            "country": _first(item.get("country")),
            # The acquisition graph, per item.
            "content_provider_host": content_host,
            "provider_verified": bool(content_url),
            "unverified_provider_hosts": rejected_hosts,
            "acquisition_note": (
                ""
                if content_url
                else (
                    f"content host(s) {', '.join(rejected_hosts)} not allowlisted"
                    if rejected_hosts
                    else "Europeana named no content object for this record"
                )
            ),
        },
    )


EUROPEANA_CAPABILITIES = SourceCapabilities(
    provider_id="europeana",
    #: Nothing unconditionally. Every role Europeana fills needs the key,
    #: which is why the plan reports READY_WITH_CREDENTIALS rather than
    #: BLOCKED: nothing is broken, a credential is simply not configured.
    capabilities=frozenset(),
    credentialled_capabilities=frozenset(
        {Capability.DISCOVERY, Capability.METADATA, Capability.RIGHTS_EVIDENCE}
    ),
    credential_env="EUROPEANA_API_KEY",
    rights_trust=0.6,
    content_hosts=("api.europeana.eu",),
    notes=(
        "A BROKER. Europeana describes objects held by hundreds of institutions and "
        "hosts almost none of them, so it declares no CONTENT_HOSTING: the content "
        "role is filled by whichever provider edmIsShownBy names, and that host must "
        "be allowlisted on its own merits before anything is fetched from it. An "
        "aggregator that can redirect the harvester anywhere IS an allowlist bypass. "
        "Its metadata is CC0, which says nothing whatsoever about the objects."
    ),
)


class CredentialsRequired(RuntimeError):
    """A configured source needs a credential that is not set."""


@register_adapter
class EuropeanaAdapter(SourceAdapter):
    """
    Options:
      `query`         -- Europeana search query
      `rows`          -- records per page (default 50)
      `api_key_env`   -- environment variable holding the key
                         (default EUROPEANA_API_KEY)
    """

    adapter_name = "europeana"
    capabilities = EUROPEANA_CAPABILITIES

    def api_key(self) -> str:
        """
        The key, or "" when it is not configured.

        Returns rather than raises so that `corpus health` can report
        READY_WITH_CREDENTIALS by asking a question, instead of having to
        catch an exception to find out that a credential is missing.
        Discovery still refuses to run without one.
        """
        env_name = self.config.option("api_key_env", "EUROPEANA_API_KEY")
        return os.environ.get(env_name, "").strip()

    def has_credentials(self) -> bool:
        return bool(self.api_key())

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        key = self.api_key()
        if not key:
            env_name = self.config.option("api_key_env", "EUROPEANA_API_KEY")
            raise CredentialsRequired(
                f"Europeana needs an API key in ${env_name}. Register for one at "
                "https://pro.europeana.eu/page/apis -- the source is configured and "
                "ready, only the credential is missing."
            )

        query = self.config.option("query", 'TYPE:TEXT AND RIGHTS:*creativecommons*')
        rows = int(self.config.option("rows", 50))

        url = build_search_url(self.config.base_url, query, key, rows, cursor or "*")
        response = self.fetch_metadata(url, conditional=False)
        candidates, next_cursor = parse_search_response(
            response.content, self.config.source_id, self.config.allowed_hosts
        )
        return DiscoveryResult(
            candidates=candidates,
            next_cursor=next_cursor or None,
            exhausted=not next_cursor,
        )


__all__ = [
    "EUROPEANA_CAPABILITIES",
    "EUROPEANA_METADATA_LICENSE",
    "CredentialsRequired",
    "EUROPEANA_METADATA_NOTE",
    "EuropeanaAdapter",
    "build_search_url",
    "parse_search_response",
]
