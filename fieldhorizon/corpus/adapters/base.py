"""
The adapter contract.

An adapter's entire job is to turn one institution's official catalogue
interface into `Candidate` objects. It has three hard boundaries:

1. **Discovery never downloads content.** `discover()` reads catalogue
   records. Content is fetched later, by the orchestrator, only for
   candidates that passed the rights engine and the curator.
2. **An adapter never adjudicates rights.** It reports what the provider
   said, in `RightsSignal`, including where it said it. The verdict is
   rights.py's alone.
3. **An adapter never constructs its own HTTP client.** It uses the
   injected `BaseFetcher`, which is what makes rate limiting, robots.txt,
   redirect validation, and offline testing uniform and unforgettable.

Adapters are resumable via an opaque `cursor` string they define and the
orchestrator merely persists.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..capabilities import Capability, SourceCapabilities
from ..http import BaseFetcher
from ..models import Candidate

logger = logging.getLogger(__name__)


@dataclass
class SourceConfig:
    """
    One source's resolved configuration, from config/corpus_sources.yaml.

    `allowed_hosts` is mandatory and has no default: an adapter with no
    host allowlist can fetch anywhere, and a source whose domains nobody
    wrote down has not been reviewed.
    """

    source_id: str
    adapter: str
    display_name: str
    enabled: bool
    base_url: str
    allowed_hosts: list[str]
    #: 0..1 confidence that this provider's *content rights statements*
    #: are reliable. Distinct from whether the catalogue is good: Internet
    #: Archive scores high for discovery and low here.
    trust: float = 0.5
    #: When False, the rights engine refuses to accept on this source's
    #: word alone (Internet Archive's default).
    trusted_for_content_rights: bool = True
    requests_per_minute: float = 20.0
    max_download_bytes: int = 64 * 1024 * 1024
    allow_http: bool = False
    languages: list[str] = field(default_factory=list)
    #: Adapter-specific settings (search queries, OAI sets, collections).
    options: dict = field(default_factory=dict)
    notes: str = ""
    #: Absolute directory for adapter-side caches, resolved from
    #: AppConfig.root by policy.load_sources. Absolute on purpose: a
    #: relative default resolves against the CURRENT WORKING DIRECTORY,
    #: which meant the offline test suite wrote its fixture catalogue
    #: into the real data/corpus/manifests/ and a later live run read it
    #: back as though it were Project Gutenberg's own catalogue --
    #: mislabelling a real document with a fixture's title.
    cache_dir: Path | None = None

    def option(self, key: str, default=None):
        return self.options.get(key, default)


@dataclass
class DiscoveryResult:
    """One page of discovery, plus the cursor needed to continue."""

    candidates: list[Candidate]
    next_cursor: str | None = None
    exhausted: bool = False


class SourceAdapter:
    """
    Base class. Subclasses implement `discover_page`.

    `discover` handles pagination, the per-run candidate budget, and
    error containment, so no adapter has to reimplement the loop.
    """

    #: Human-readable name used in configuration and reports.
    adapter_name = "base"

    #: What this adapter actually does. Declared by the class, not
    #: inferred: an adapter that claims CONTENT_HOSTING and then cannot
    #: serve bytes is a bug we want to see as a plan failure naming that
    #: provider, rather than as a mysterious download error. Adapters
    #: that have not been given a declaration yet fall back to the
    #: Phase I assumption of doing everything, which is what they were
    #: written against.
    capabilities: SourceCapabilities | None = None

    @classmethod
    def declared_capabilities(cls, provider_id: str) -> SourceCapabilities:
        if cls.capabilities is not None:
            return cls.capabilities
        return SourceCapabilities(
            provider_id=provider_id,
            capabilities=frozenset(
                {
                    Capability.DISCOVERY,
                    Capability.METADATA,
                    Capability.RIGHTS_EVIDENCE,
                    Capability.CONTENT_HOSTING,
                }
            ),
            notes="Phase I adapter: assumed to fill every role until it declares otherwise.",
        )

    def __init__(self, config: SourceConfig, fetcher: BaseFetcher) -> None:
        self.config = config
        self.fetcher = fetcher

    # ---------------------------------------------------------------- api

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        """
        Fetch one page of catalogue records.

        Must return `exhausted=True` (or `next_cursor=None`) when the
        catalogue is finished, or discovery will loop forever.
        """
        raise NotImplementedError

    def discover(self, cursor: str | None = None, max_candidates: int = 500) -> Iterator[Candidate]:
        """
        Walk the catalogue, yielding candidates until exhausted or the
        budget is reached.

        A page that raises stops discovery for THIS source and lets the
        exception propagate to the orchestrator, which records it against
        this source alone and continues with the others -- one dead
        endpoint must never abort a whole harvest.
        """
        seen = 0
        pages = 0
        while seen < max_candidates:
            result = self.discover_page(cursor)
            pages += 1
            for candidate in result.candidates:
                yield candidate
                seen += 1
                if seen >= max_candidates:
                    break
            if result.exhausted or not result.next_cursor:
                break
            if result.next_cursor == cursor:
                logger.warning(
                    "%s: cursor did not advance (%r); stopping to avoid an infinite loop",
                    self.config.source_id, cursor,
                )
                break
            cursor = result.next_cursor
            # A hard page ceiling as a second safety net: a catalogue that
            # keeps handing back a fresh cursor with zero records would
            # otherwise spin until the item budget is met, which it never
            # would be.
            if pages > 1000:
                logger.warning("%s: page limit reached during discovery", self.config.source_id)
                break

    def fetch_content(self, candidate: Candidate, max_bytes: int = 0):
        """
        Download one candidate's content.

        The default implementation is correct for every source that
        exposes a direct download URL in its catalogue record. Sources
        needing a second lookup (Internet Archive's file list, Wikisource's
        page assembly) override this.
        """
        if not candidate.download_url:
            raise ValueError(f"Candidate {candidate.document_key()} has no download URL")
        return self.fetcher.fetch(
            candidate.download_url,
            allowed_hosts=self.config.allowed_hosts,
            max_bytes=max_bytes or self.config.max_download_bytes,
            allow_http=self.config.allow_http,
        )

    # ------------------------------------------------------------ helpers

    def fetch_metadata(self, url: str, *, conditional: bool = True, accept: str = ""):
        return self.fetcher.fetch(
            url,
            allowed_hosts=self.config.allowed_hosts,
            max_bytes=self.config.max_download_bytes,
            conditional=conditional,
            allow_http=self.config.allow_http,
            accept=accept,
        )


#: Registry of adapter classes by name. Populated by adapters/registry.py.
ADAPTER_REGISTRY: dict[str, type[SourceAdapter]] = {}


def register_adapter(cls: type[SourceAdapter]) -> type[SourceAdapter]:
    ADAPTER_REGISTRY[cls.adapter_name] = cls
    return cls


__all__ = ["ADAPTER_REGISTRY", "DiscoveryResult", "SourceAdapter", "SourceConfig", "register_adapter"]
