"""
Wikisource, via the MediaWiki API (targeted/incremental) or dumps
(archive profile).

Wikisource content is CC BY-SA 4.0 by the site's own terms, with many
individual texts additionally in the public domain. That means:

* **Attribution and share-alike obligations are real and are recorded.**
  Unlike CC0 sources, a Wikisource text carries obligations that must
  survive into any export, so the adapter records the page URL (which is
  the canonical attribution target, per Wikimedia's own guidance) and the
  licence URL as evidence.
* The site-wide licence is a genuine content licence, not a
  metadata-only licence -- Wikisource *is* the content.

Editorial exclusions, all implemented in `wikimedia.is_importable_page`:
talk pages, categories, indexes, templates, non-content namespaces, stubs,
and subpages of works already imported via their parent.
"""

from __future__ import annotations

import json
import logging

from ..language import normalize_language_code
from ..models import Candidate, RightsSignal
from ..rights import evidence_from_field
from .base import DiscoveryResult, SourceAdapter, register_adapter
from .wikimedia import build_category_query, build_content_query, clean_wikitext, is_importable_page

logger = logging.getLogger(__name__)

CC_BY_SA_URL = "https://creativecommons.org/licenses/by-sa/4.0/"

WIKISOURCE_LICENSE_STATEMENT = (
    "Text on Wikisource is available under the Creative Commons Attribution-ShareAlike "
    "4.0 licence; individual works may additionally be in the public domain. Attribution "
    "is to the page and its contributor history."
)


def parse_category_members(payload: bytes) -> tuple[list[dict], str]:
    """Returns (members, continue_token). Empty token means exhausted."""
    data = json.loads(payload.decode("utf-8", errors="replace"))
    members = data.get("query", {}).get("categorymembers", [])
    token = data.get("continue", {}).get("cmcontinue", "")
    return [m for m in members if isinstance(m, dict)], token


def parse_page_content(payload: bytes) -> list[dict]:
    """
    Extract `{title, pageid, ns, wikitext, url}` from an `action=query`
    revisions response. Missing pages and pages with no revision content
    are skipped rather than raising -- a title can vanish between the
    category listing and the content fetch.
    """
    data = json.loads(payload.decode("utf-8", errors="replace"))
    pages = data.get("query", {}).get("pages", [])
    if isinstance(pages, dict):  # formatversion=1 shape, defensively handled
        pages = list(pages.values())

    out: list[dict] = []
    for page in pages:
        if not isinstance(page, dict) or page.get("missing"):
            continue
        revisions = page.get("revisions") or []
        if not revisions:
            continue
        revision = revisions[0]
        content = ""
        slots = revision.get("slots") or {}
        if isinstance(slots, dict):
            main = slots.get("main") or {}
            content = main.get("content", "") if isinstance(main, dict) else ""
        if not content:
            content = revision.get("content", "") or revision.get("*", "")
        if not content:
            continue
        out.append(
            {
                "title": page.get("title", ""),
                "pageid": str(page.get("pageid", "")),
                "ns": int(page.get("ns", 0) or 0),
                "wikitext": content,
                "url": page.get("fullurl", ""),
                "timestamp": revision.get("timestamp", ""),
            }
        )
    return out


def candidate_from_page(page: dict, source_id: str, base_url: str, language: str) -> Candidate | None:
    """
    Build a candidate from a fetched page, applying the editorial filter.

    Wikisource is unusual among these sources: the content arrives during
    *discovery*, because the API returns wikitext alongside the metadata.
    The cleaned text is stashed in `raw_metadata["_content"]` and
    `fetch_content` returns it without a second request, which is both
    faster and one fewer request against Wikimedia's servers.
    """
    title = page.get("title", "")
    wikitext = page.get("wikitext", "")

    importable, reason = is_importable_page(title, page.get("ns", 0), wikitext)
    if not importable:
        logger.debug("Wikisource: skipping %r (%s)", title, reason)
        return None

    text, headings = clean_wikitext(wikitext)
    if len(text.strip()) < 800:
        logger.debug("Wikisource: skipping %r (no substantial content after cleaning)", title)
        return None

    page_url = page.get("url") or f"{base_url.rstrip('/')}/wiki/{title.replace(' ', '_')}"

    return Candidate(
        source_id=source_id,
        external_id=page.get("pageid") or title,
        title=title,
        language=normalize_language_code(language),
        edition_date=page.get("timestamp", ""),
        document_type="wikisource_page",
        canonical_url=page_url,
        download_url=page_url,
        download_format="txt",
        estimated_bytes=len(text.encode("utf-8")),
        work_identifiers=(f"wikisource:{page.get('pageid')}",) if page.get("pageid") else (),
        rights=RightsSignal(
            content_license="CC BY-SA 4.0",
            rights_statement_uri=CC_BY_SA_URL,
            provider_declared_scope="WORLDWIDE",
            evidence=(
                evidence_from_field("wikisource:site-license", WIKISOURCE_LICENSE_STATEMENT, CC_BY_SA_URL),
                evidence_from_field("wikisource:page-attribution", f"Wikisource page: {title}", page_url),
            ),
            raw_rights_text="CC BY-SA 4.0 (Wikisource site licence)",
        ),
        raw_metadata={
            "title": title,
            "pageid": page.get("pageid"),
            "headings": headings[:40],
            # Carried through discovery so fetch_content needs no request.
            "_content": text,
        },
    )


@register_adapter
class WikisourceAdapter(SourceAdapter):
    """
    Options:
      `language`   -- subdomain language code (default "en")
      `categories` -- list of category names to walk
      `page_size`  -- category members per API page (default 50)
      `mode`       -- "api" (default) or "dump"
    """

    adapter_name = "wikisource"

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        categories = self.config.option("categories", []) or []
        if not categories:
            logger.warning("%s: no categories configured; nothing to discover", self.config.source_id)
            return DiscoveryResult([], None, exhausted=True)

        # Cursor encodes which category we are in and where within it, so
        # a run interrupted halfway through the third category resumes
        # there rather than restarting at the first.
        category_index, continue_token = _decode_cursor(cursor)
        if category_index >= len(categories):
            return DiscoveryResult([], None, exhausted=True)

        language = self.config.option("language", "en")
        base = self.config.base_url
        category = categories[category_index]

        listing = self.fetch_metadata(
            build_category_query(base, category, continue_token, int(self.config.option("page_size", 50)))
        )
        members, next_token = parse_category_members(listing.content)

        candidates: list[Candidate] = []
        titles = [m.get("title", "") for m in members if m.get("title")]
        for batch_start in range(0, len(titles), 20):
            batch = titles[batch_start : batch_start + 20]
            if not batch:
                continue
            content_response = self.fetch_metadata(build_content_query(base, batch))
            for page in parse_page_content(content_response.content):
                candidate = candidate_from_page(page, self.config.source_id, base, language)
                if candidate is not None:
                    candidates.append(candidate)

        if next_token:
            next_cursor = _encode_cursor(category_index, next_token)
            exhausted = False
        elif category_index + 1 < len(categories):
            next_cursor = _encode_cursor(category_index + 1, "")
            exhausted = False
        else:
            next_cursor = None
            exhausted = True

        return DiscoveryResult(candidates=candidates, next_cursor=next_cursor, exhausted=exhausted)

    def fetch_content(self, candidate: Candidate, max_bytes: int = 0):
        """
        Return the text captured during discovery, with no new request.

        Wikisource is the one source whose content arrives with its
        metadata; re-fetching would be a pointless second hit on
        Wikimedia's API for bytes already in hand.
        """
        from ..http import FetchResponse
        from ..security import sha256_bytes

        content = (candidate.raw_metadata or {}).get("_content", "")
        if not content:
            return super().fetch_content(candidate, max_bytes)
        data = content.encode("utf-8")
        return FetchResponse(
            url=candidate.download_url,
            final_url=candidate.canonical_url,
            status_code=200,
            content=data,
            headers={},
            detected_mime="text/plain",
            declared_mime="text/plain",
            encoding="utf-8",
            sha256=sha256_bytes(data),
            from_cache=True,
        )


def _encode_cursor(category_index: int, token: str) -> str:
    return f"{category_index}|{token}"


def _decode_cursor(cursor: str | None) -> tuple[int, str]:
    if not cursor:
        return 0, ""
    index_part, _, token = cursor.partition("|")
    try:
        return int(index_part), token
    except ValueError:
        return 0, ""


__all__ = [
    "CC_BY_SA_URL",
    "WIKISOURCE_LICENSE_STATEMENT",
    "WikisourceAdapter",
    "candidate_from_page",
    "parse_category_members",
    "parse_page_content",
]
