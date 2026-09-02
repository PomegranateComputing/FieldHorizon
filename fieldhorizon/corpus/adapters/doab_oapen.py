"""
The DOAB -> OAPEN bridge: two providers, four roles, and a gap.

DOAB is a directory. OAPEN is a repository. Phase I treated each as a
self-contained source and got a stream of documents it could not
adjudicate; this adapter states the division explicitly:

    DOAB    DISCOVERY, METADATA          directory.doabooks.org
    OAPEN   CONTENT_HOSTING, RENDERED_TEXT   library.oapen.org
    ----    RIGHTS_EVIDENCE              nobody

**The gap is the finding, and it is measured.** Probed live on
2026-08-01:

    DOAB OAI-PMH, oai_dc, 100 records     0 carried <dc:rights>
    DOAB REST, 12 items sampled           6 no rights field
                                          5 "open access"
                                          0 a licence URI
    OAPEN REST, 10 items sampled          0 rights metadata of any kind

"Open access" is a business model. It says the reader pays nothing; it
grants no permission to redistribute, adapt, or index, and it is not a
licence. So under this project's rules every document reached this way
quarantines -- not because the bridge failed, but because it worked and
the answer is that no provider fills the rights role. That is exactly
what the capability graph was built to say out loud.

It is worth being clear that this is not a criticism of either service.
Both are excellent at what they publish. The licence simply lives on the
publisher's own page for most of these titles, and neither aggregator
republishes it in a machine-readable field.

**One thing OAPEN gives that nothing else in this harvester does.** Its
items carry a `TEXT` bundle -- the PDF's text layer, already extracted,
served as `text/plain`. That is a genuine RENDERED_TEXT capability: no
PDF parsing, no OCR, no quality gamble. It is preferred over the
`ORIGINAL` PDF whenever present.

**Boundaries.** The OAPEN web interface is never scraped; this uses the
published OAI-PMH and REST interfaces. DOAB's `/api/search` answers 403
with a Cloudflare interstitial and is therefore not used at all --
`/rest/search` and `/oai/request` answer normally and are what the
adapter talks to. No 403 is worked around, and no rotating proxy exists
anywhere in this codebase.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

from ..capabilities import Capability, SourceCapabilities
from ..content_signal import ContentSignal
from ..http import FetchError
from ..lattice import ComponentRights, RightsComponent, RightsStack
from ..models import ACCESS_BLOCKED_RETRY_AFTER_SECONDS, Candidate, Evidence, RightsSignal
from ..rights import evidence_from_field
from .base import DiscoveryResult, register_adapter
from .oai_pmh import OAIPMHAdapter, build_list_records_url, build_resume_url, parse_oai_response
from .oapen_doab import refine_rights

logger = logging.getLogger(__name__)

DOAB_BASE = "https://directory.doabooks.org"
OAPEN_BASE = "https://library.oapen.org"

#: An OAPEN handle inside a DOAB `dc:identifier`. DOAB publishes both the
#: landing page and, usually, a direct bitstream URL; the handle is what
#: both share and is what the REST lookup needs.
_OAPEN_HANDLE_RE = re.compile(r"library\.oapen\.org/handle/([\w.]+/\d+)", re.IGNORECASE)

#: DSpace bundle names, in the order this harvester wants them. TEXT is
#: the PDF's already-extracted text layer: preferring it over ORIGINAL
#: skips PDF parsing entirely and is the single biggest quality win
#: available on this source.
_BUNDLE_PREFERENCE = ("TEXT", "ORIGINAL")

#: Within a bundle, which MIME types are usable and in what order.
_MIME_PREFERENCE = ("text/plain", "application/epub+zip", "application/pdf")

DOAB_CAPABILITIES = SourceCapabilities(
    provider_id="doab",
    capabilities=frozenset({Capability.DISCOVERY, Capability.METADATA}),
    rights_trust=0.5,
    content_hosts=("directory.doabooks.org",),
    notes=(
        "A directory, not a repository. Declares NO rights evidence: across 100 OAI "
        "records none carried dc:rights, and of 12 items sampled through the REST API "
        "none published a licence URI -- five said 'open access', which is an access "
        "model and grants nothing. Its robots.txt declares "
        "Content-Signal: search=yes,ai-train=no,use=reference, which is recorded "
        "against every document taken through this bridge."
    ),
)

OAPEN_CAPABILITIES = SourceCapabilities(
    provider_id="oapen",
    capabilities=frozenset(
        {Capability.CONTENT_HOSTING, Capability.RENDERED_TEXT, Capability.METADATA}
    ),
    rights_trust=0.5,
    content_hosts=("library.oapen.org",),
    notes=(
        "Hosts the full text and, unusually, publishes an already-extracted text layer "
        "in a TEXT bundle -- no PDF parsing and no OCR needed. Declares no rights "
        "evidence: of 10 items sampled, none carried rights metadata of any kind."
    ),
)


# --------------------------------------------------------------------------
# The bridge
# --------------------------------------------------------------------------


def oapen_handle_from(identifiers: list[str]) -> str:
    """The OAPEN handle a DOAB record points at, or "" when it points nowhere."""
    for value in identifiers:
        match = _OAPEN_HANDLE_RE.search(value or "")
        if match:
            return match.group(1)
    return ""


@dataclass(frozen=True)
class Bitstream:
    """One file attached to an OAPEN item."""

    name: str
    bundle: str
    mime: str
    size: int
    retrieve_link: str

    @property
    def url(self) -> str:
        if self.retrieve_link.startswith(("http://", "https://")):
            return self.retrieve_link
        return f"{OAPEN_BASE}{self.retrieve_link}"


def parse_oapen_item(payload: bytes) -> tuple[dict[str, list[str]], list[Bitstream]]:
    """
    An OAPEN REST item: its metadata and its bitstreams.

    Returns `({key: [values]}, [Bitstream])`. A search that matched
    nothing returns empty rather than raising -- a DOAB record pointing
    at an OAPEN handle that no longer resolves is a broken link, which is
    a per-item condition, not a run-ending one.
    """
    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"OAPEN REST returned non-JSON ({exc})") from exc

    if isinstance(data, dict):
        data = [data]
    if not data:
        return {}, []

    item = data[0]
    metadata: dict[str, list[str]] = {}
    for entry in item.get("metadata") or []:
        if isinstance(entry, dict) and entry.get("key"):
            metadata.setdefault(str(entry["key"]), []).append(str(entry.get("value", "")))

    bitstreams = [
        Bitstream(
            name=str(b.get("name", "")),
            bundle=str(b.get("bundleName", "")),
            mime=str(b.get("mimeType", "")),
            size=int(b.get("sizeBytes", 0) or 0),
            retrieve_link=str(b.get("retrieveLink", "")),
        )
        for b in item.get("bitstreams") or []
        if isinstance(b, dict)
    ]
    return metadata, bitstreams


def choose_bitstream(bitstreams: list[Bitstream]) -> Bitstream | None:
    """
    The best file to ingest.

    TEXT beats ORIGINAL: OAPEN has already extracted the PDF's text
    layer, and using it means never parsing a PDF, never guessing whether
    a scan has a usable text layer, and never reaching for OCR. Within a
    bundle, plain text beats EPUB beats PDF.

    THUMBNAIL and EXPORT bundles are excluded entirely -- a `.jpg` cover
    and a `.ris` citation record are not the book.
    """
    for bundle in _BUNDLE_PREFERENCE:
        in_bundle = [b for b in bitstreams if b.bundle == bundle and b.size > 0]
        if not in_bundle:
            continue
        for mime in _MIME_PREFERENCE:
            matching = [b for b in in_bundle if b.mime == mime]
            if matching:
                # Largest wins: OAPEN sometimes attaches text for both the
                # whole book and a single chapter, and the book is wanted.
                return max(matching, key=lambda b: b.size)
    return None


def rights_stack_for(
    doab_rights: list[str],
    oapen_metadata: dict[str, list[str]],
    *,
    doab_url: str,
    oapen_url: str,
    signal: ContentSignal | None = None,
) -> RightsStack:
    """
    State what each provider said, including that neither stated a licence.

    The evidence records the *absence* explicitly rather than leaving the
    stack empty. "We looked at both providers and neither published a
    per-book licence" is a finding an operator can act on -- by going to
    the publisher's page for that title -- whereas an empty stack is
    indistinguishable from a parser that failed to run.
    """
    evidence: list[Evidence] = []
    if doab_rights:
        evidence.append(
            evidence_from_field("doab:dc:rights", " | ".join(doab_rights)[:500], doab_url)
        )
    else:
        evidence.append(
            evidence_from_field(
                "doab:dc:rights:absent",
                "The DOAB record published no dc:rights element.",
                doab_url,
            )
        )

    oapen_rights = oapen_metadata.get("dc.rights.uri", []) + oapen_metadata.get("dc.rights", [])
    if oapen_rights:
        evidence.append(
            evidence_from_field("oapen:dc.rights", " | ".join(oapen_rights)[:500], oapen_url)
        )
    else:
        evidence.append(
            evidence_from_field(
                "oapen:dc.rights:absent",
                "The OAPEN item published no rights metadata.",
                oapen_url,
            )
        )

    if signal is not None and signal.declared:
        evidence.append(
            evidence_from_field(
                "doab:robots:content-signal",
                f"{signal.raw} -- published conditions of use, recorded as obligations "
                f"{', '.join(signal.obligations()) or '(none)'}.",
                f"https://{signal.host}/robots.txt",
            )
        )

    # Only a value that names a licence goes in. "Open access" and
    # `info:eu-repo/semantics/openAccess` are access models: they say the
    # reader pays nothing, not that anyone may redistribute.
    stated = _first_licence(doab_rights + oapen_rights)

    stack = RightsStack()
    stack.add(
        RightsComponent.WORK,
        stated,
        evidence=tuple(evidence),
        notes=(
            "licence stated by the provider"
            if stated
            else "neither DOAB nor OAPEN published a per-book licence; "
            "the publisher's own page is the place to look"
        ),
    )
    stack.set(
        ComponentRights.absent(
            RightsComponent.DIGITAL_EDITION,
            "no separate statement covers the digitisation",
        )
    )
    return stack


_ACCESS_MODEL_ONLY = re.compile(
    r"^\s*(info:eu-repo/semantics/(open|embargoed|restricted)access|open\s*access|"
    r"libre\s*acc[eè]s|freier\s*zugang|acceso\s*abierto|accesso\s*aperto)\s*$",
    re.IGNORECASE,
)
_NAMES_A_LICENCE = re.compile(
    r"creativecommons\.org/(?:licenses|publicdomain)/|rightsstatements\.org/vocab/|"
    r"\bCC[ -]?(?:BY|0)\b|public\s+domain",
    re.IGNORECASE,
)


def _first_licence(values: list[str]) -> str:
    for value in values:
        if not value or _ACCESS_MODEL_ONLY.match(value):
            continue
        if _NAMES_A_LICENCE.search(value):
            return value
    return ""


@register_adapter
class DoabOapenBridgeAdapter(OAIPMHAdapter):
    """
    Options:
      `metadata_prefix`, `set`, `from`  inherited from the OAI-PMH adapter
      `resolve_content`  when False, discovery stops at DOAB metadata and
                         every candidate rests in METADATA_ONLY
      `max_resolutions`  ceiling on OAPEN lookups per page
    """

    adapter_name = "doab_oapen"
    capabilities = DOAB_CAPABILITIES

    def __init__(self, config, fetcher) -> None:
        super().__init__(config, fetcher)
        #: Handles OAPEN refused with a 403. Recorded so the orchestrator
        #: can put them in ACCESS_BLOCKED and retry later, rather than
        #: burning them as permanent failures -- and so nothing here is
        #: ever tempted to route around the refusal.
        self.access_blocked: list[dict] = []
        #: Records DOAB published without an OAPEN link at all.
        self.without_content: int = 0

    # ------------------------------------------------------------ signal

    def content_signal(self) -> ContentSignal | None:
        """
        The host's declared conditions of use, if the fetcher read them.

        Read through the fetcher's robots cache so this costs no extra
        request; absent when robots.txt was unreachable.
        """
        getter = getattr(self.fetcher, "content_signal_for", None)
        if getter is None:
            return None
        try:
            return getter(f"{DOAB_BASE}/")
        except Exception:  # noqa: BLE001 -- a missing signal must never stop a harvest
            return None

    # --------------------------------------------------------- discovery

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        base = self.config.base_url or f"{DOAB_BASE}/oai/request"
        url = (
            build_resume_url(base, cursor)
            if cursor
            else build_list_records_url(
                base,
                self.config.option("metadata_prefix", "oai_dc"),
                self.config.option("set", ""),
                self.config.option("from", ""),
            )
        )

        response = self.fetch_metadata(url, accept="application/xml")
        candidates, token = parse_oai_response(response.content, self.config.source_id, base)

        signal = self.content_signal()
        resolve = bool(self.config.option("resolve_content", True))
        budget = int(self.config.option("max_resolutions", 25))

        out: list[Candidate] = []
        resolved = 0
        for candidate in candidates:
            refined = refine_rights(candidate, self.config.source_id)
            handle = oapen_handle_from(
                [str(v) for v in (candidate.raw_metadata.get("identifier") or [])]
                + [candidate.download_url, candidate.canonical_url]
            )
            if not handle:
                self.without_content += 1
                out.append(self._as_metadata_only(refined, signal))
                continue
            if not resolve or resolved >= budget:
                out.append(self._as_metadata_only(refined, signal))
                continue
            resolved += 1
            out.append(self._bridge(refined, handle, signal))

        logger.info(
            "%s: %d record(s), %d resolved to OAPEN, %d with no content provider, %d blocked",
            self.config.source_id, len(candidates), resolved,
            self.without_content, len(self.access_blocked),
        )
        return DiscoveryResult(candidates=out, next_cursor=token, exhausted=token is None)

    # ----------------------------------------------------------- bridge

    def _bridge(self, candidate: Candidate, handle: str, signal: ContentSignal | None) -> Candidate:
        """Resolve one DOAB record to OAPEN's full text."""
        query = f"{OAPEN_BASE}/rest/search?query=handle:%22{quote(handle)}%22&expand=metadata,bitstreams"
        try:
            response = self.fetcher.fetch(
                query,
                allowed_hosts=self.config.allowed_hosts,
                max_bytes=self.config.max_download_bytes,
            )
        except FetchError as exc:
            if exc.status_code == 403:
                # Recorded and left alone. A 403 is the provider saying
                # no; the only correct responses are to wait and to ask
                # again later, and this project does neither by pretending
                # to be something else.
                self.access_blocked.append(
                    {
                        "handle": handle,
                        "url": query,
                        "retry_after_seconds": ACCESS_BLOCKED_RETRY_AFTER_SECONDS,
                    }
                )
                logger.info("OAPEN refused %s with 403; recorded as ACCESS_BLOCKED", handle)
                return self._as_metadata_only(candidate, signal, blocked=True)
            logger.info("OAPEN lookup for %s failed (%s)", handle, exc)
            return self._as_metadata_only(candidate, signal)

        try:
            metadata, bitstreams = parse_oapen_item(response.content)
        except ValueError as exc:
            logger.info("OAPEN item %s unparseable (%s)", handle, exc)
            return self._as_metadata_only(candidate, signal)

        chosen = choose_bitstream(bitstreams)
        if chosen is None:
            return self._as_metadata_only(candidate, signal)

        oapen_url = f"{OAPEN_BASE}/handle/{handle}"
        stack = rights_stack_for(
            [str(v) for v in (candidate.raw_metadata.get("rights") or [])],
            metadata,
            doab_url=candidate.canonical_url,
            oapen_url=oapen_url,
            signal=signal,
        )
        return self._with(
            candidate,
            download_url=chosen.url,
            download_format=_format_for(chosen.mime),
            estimated_bytes=chosen.size,
            stack=stack,
            signal=signal,
            extra={
                "oapen_handle": handle,
                "oapen_url": oapen_url,
                "bitstream": {
                    "name": chosen.name, "bundle": chosen.bundle,
                    "mime": chosen.mime, "size": chosen.size,
                },
                "pre_extracted_text": chosen.bundle == "TEXT",
                "content_provider": "oapen",
            },
        )

    def _as_metadata_only(
        self, candidate: Candidate, signal: ContentSignal | None, *, blocked: bool = False
    ) -> Candidate:
        """
        A record with metadata and no reachable content.

        Not a failure and not discarded: the catalogue entry is real and
        the rights position is unchanged. It simply has no content
        provider, which is a state.
        """
        stack = rights_stack_for(
            [str(v) for v in (candidate.raw_metadata.get("rights") or [])],
            {},
            doab_url=candidate.canonical_url,
            oapen_url="",
            signal=signal,
        )
        return self._with(
            candidate,
            download_url="",
            download_format="",
            estimated_bytes=0,
            stack=stack,
            signal=signal,
            extra={
                "content_provider": "",
                "access_blocked": blocked,
                "acquisition_note": (
                    "OAPEN refused the lookup with 403; recorded for a later retry"
                    if blocked
                    else "no content provider found for this record"
                ),
            },
        )

    def _with(
        self, candidate: Candidate, *, download_url: str, download_format: str,
        estimated_bytes: int, stack: RightsStack, signal: ContentSignal | None, extra: dict,
    ) -> Candidate:
        work = stack.get(RightsComponent.WORK)
        raw = dict(candidate.raw_metadata or {})
        raw.update(extra)
        raw["rights_components"] = stack.to_dict()
        if signal is not None and signal.declared:
            raw["content_signal"] = signal.to_dict()
            raw["use_obligations"] = list(signal.obligations())

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
            download_url=download_url,
            download_format=download_format,
            estimated_bytes=estimated_bytes,
            work_identifiers=candidate.work_identifiers,
            alternate_downloads=candidate.alternate_downloads,
            rights=RightsSignal(
                content_license=work.raw_license if work else "",
                metadata_license=candidate.rights.metadata_license,
                rights_statement_uri=candidate.rights.rights_statement_uri,
                provider_declared_scope="",
                evidence=work.evidence if work else (),
                access_restricted=not download_url,
                raw_rights_text=candidate.rights.raw_rights_text,
            ),
            raw_metadata=raw,
        )


def _format_for(mime: str) -> str:
    return {
        "text/plain": "txt",
        "application/pdf": "pdf",
        "application/epub+zip": "epub",
    }.get(mime, "")


__all__ = [
    "DOAB_CAPABILITIES",
    "OAPEN_CAPABILITIES",
    "Bitstream",
    "DoabOapenBridgeAdapter",
    "choose_bitstream",
    "oapen_handle_from",
    "parse_oapen_item",
    "rights_stack_for",
]
