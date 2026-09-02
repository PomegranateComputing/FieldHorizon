"""
Adapter registry.

Importing this module is what populates `ADAPTER_REGISTRY` -- each
adapter module registers itself via the `@register_adapter` decorator at
import time, so the registry is assembled by importing the package rather
than by maintaining a parallel list that drifts.

There is deliberately **no generic web-scraper adapter**, and adding one
would violate the project's rules. A new source becomes available by
writing an adapter (or, for OPDS/OAI-PMH/SRU sources, by configuration
alone), never by pointing a crawler at a website.
"""

from __future__ import annotations

from ..http import BaseFetcher

# Import for side effects: each module registers its adapter class.
from . import (  # noqa: F401  (imported for registration side effects)
    doab_oapen,
    europeana,
    gallica,
    gallica_text,
    gutenberg,
    internet_archive,
    oai_pmh,
    oapen_doab,
    opds,
    standard_ebooks,
    standard_ebooks_github,
    wikisource,
    wikisource_dump,
)
from .base import ADAPTER_REGISTRY, SourceAdapter, SourceConfig


class UnknownAdapterError(KeyError):
    """The configuration named an adapter that does not exist."""


def available_adapters() -> list[str]:
    return sorted(ADAPTER_REGISTRY)


def build_adapter(config: SourceConfig, fetcher: BaseFetcher) -> SourceAdapter:
    adapter_cls = ADAPTER_REGISTRY.get(config.adapter)
    if adapter_cls is None:
        raise UnknownAdapterError(
            f"Unknown adapter {config.adapter!r} for source {config.source_id!r}. "
            f"Available: {', '.join(available_adapters())}"
        )
    return adapter_cls(config, fetcher)


__all__ = ["ADAPTER_REGISTRY", "UnknownAdapterError", "available_adapters", "build_adapter"]
