"""
Shared MediaWiki machinery: API access, dump reading, and wikitext
cleaning.

This is the reusable brick; `wikisource.py` is the source definition
built on it. Two acquisition modes, per the brief:

* **API mode** for targeted and incremental acquisition. Uses
  `action=query` with continuation, which is the documented, rate-limited
  interface -- never the HTML site.
* **Dump mode** for the `archive` profile, streaming a downloaded XML
  dump with checkpointing so a 40 GB file can be processed across
  several bounded runs.

Wikitext cleaning is deliberately conservative. Templates are removed
because their expansion requires the whole MediaWiki template engine and
an unexpanded `{{...}}` in a corpus is noise; but the *text* inside link
syntax is preserved, because `[[Plato|the philosopher]]` renders as "the
philosopher" and dropping it would silently delete words from the work.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from urllib.parse import urlencode

from ..security import local_name, safe_parse_xml

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Wikitext cleaning
# --------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_REF_RE = re.compile(r"<ref[^>]*?/>|<ref[^>]*?>.*?</ref>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"</?(?:div|span|small|center|poem|noinclude|includeonly|onlyinclude|br|pages?|section)[^>]*>",
                     re.IGNORECASE)
_TABLE_RE = re.compile(r"\{\|.*?\|\}", re.DOTALL)
_FILE_LINK_RE = re.compile(r"\[\[(?:File|Image|Fichier|Datei|Archivo|Immagine):[^\]]*?\]\]", re.IGNORECASE)
_CATEGORY_RE = re.compile(r"\[\[(?:Category|Catégorie|Kategorie|Categoría|Categoria):[^\]]*?\]\]", re.IGNORECASE)
_EXTERNAL_LINK_RE = re.compile(r"\[(?:https?|ftp)://\S+?(?:\s+([^\]]*))?\]")
_INTERNAL_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]*))?\]\]")
_HEADING_RE = re.compile(r"^\s*(={2,6})\s*(.*?)\s*\1\s*$", re.MULTILINE)
_BOLD_ITALIC_RE = re.compile(r"'{2,5}")
_LIST_PREFIX_RE = re.compile(r"^[*#:;]+\s*", re.MULTILINE)
_HR_RE = re.compile(r"^-{4,}\s*$", re.MULTILINE)


def strip_templates(text: str, max_passes: int = 8) -> str:
    """
    Remove `{{...}}` including nesting.

    Iterative innermost-first removal rather than a single regex: braces
    nest arbitrarily in real Wikisource pages (a header template
    containing a formatting template containing a link template), and a
    non-recursive pattern leaves the outer braces behind.
    """
    pattern = re.compile(r"\{\{[^{}]*\}\}")
    for _ in range(max_passes):
        new_text, count = pattern.subn("", text)
        text = new_text
        if not count:
            break
    return text


def clean_wikitext(text: str) -> tuple[str, list[str]]:
    """
    Wikitext to plain text. Returns (text, headings).

    Order matters: comments and refs first (they can contain any other
    syntax), then templates and tables, then links, then inline markup.
    Doing links before templates leaves template arguments as stray text.
    """
    text = _COMMENT_RE.sub("", text)
    text = _REF_RE.sub("", text)
    text = strip_templates(text)
    text = _TABLE_RE.sub("", text)
    text = _FILE_LINK_RE.sub("", text)
    text = _CATEGORY_RE.sub("", text)

    headings = [match.group(2).strip() for match in _HEADING_RE.finditer(text) if match.group(2).strip()]
    text = _HEADING_RE.sub(lambda m: f"\n\n{m.group(2).strip()}\n\n", text)

    # Preserve the rendered text of both link forms.
    text = _EXTERNAL_LINK_RE.sub(lambda m: m.group(1) or "", text)
    text = _INTERNAL_LINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), text)

    text = _TAG_RE.sub("", text)
    text = _BOLD_ITALIC_RE.sub("", text)
    text = _LIST_PREFIX_RE.sub("", text)
    text = _HR_RE.sub("", text)
    text = re.sub(r"<[^>]{1,80}>", "", text)  # residual simple HTML tags
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip(), headings


# --------------------------------------------------------------------------
# Page filtering
# --------------------------------------------------------------------------

#: Namespaces that never contain the works themselves. Wikisource's main
#: namespace is 0; `Page:` (104) holds per-scan-page transcriptions that
#: are assembled into main-namespace works, so importing them would
#: duplicate every text at page granularity.
CONTENT_NAMESPACES = frozenset({0})

_NON_CONTENT_PREFIXES = (
    "talk:", "user:", "wikisource:", "file:", "mediawiki:", "template:", "help:",
    "category:", "portal:", "index:", "page:", "author:", "module:", "translation talk:",
    "discussion:", "utilisateur:", "modèle:", "catégorie:", "aide:", "portail:",
    "diskussion:", "benutzer:", "vorlage:", "kategorie:",
)

#: Titles that are indexes or disambiguation rather than works.
_INDEX_TITLE_RE = re.compile(
    r"\b(index|sommaire|table of contents|table des matières|disambiguation|homonymie|"
    r"list of|liste des|main page|accueil)\b",
    re.IGNORECASE,
)

#: Below this, a main-namespace page is a stub, a redirect target, or a
#: navigation shell -- not a work.
MIN_CONTENT_CHARS = 1200


def is_importable_page(title: str, namespace: int, text: str) -> tuple[bool, str]:
    """
    Returns (importable, reason_if_not).

    The subpage rule deserves a note: Wikisource splits long works into
    `Work/Chapter 1`, `Work/Chapter 2`, and so on, with the parent page
    transcluding them all. Importing both the parent and the children
    duplicates the entire work. This adapter imports the *parent* and
    lets its transclusions be resolved by the API's rendered output, so
    subpages are skipped.
    """
    lowered = (title or "").lower()

    if namespace not in CONTENT_NAMESPACES:
        return False, f"namespace {namespace} is not a content namespace"
    if any(lowered.startswith(prefix) for prefix in _NON_CONTENT_PREFIXES):
        return False, "non-content namespace prefix"
    if "/" in title:
        return False, "subpage of a parent work (imported via its parent)"
    if _INDEX_TITLE_RE.search(title):
        return False, "index, list, or disambiguation page"
    if text.strip().lower().startswith("#redirect") or text.strip().lower().startswith("#redirection"):
        return False, "redirect"
    if len(text.strip()) < MIN_CONTENT_CHARS:
        return False, f"insufficient content ({len(text.strip())} chars)"
    return True, ""


# --------------------------------------------------------------------------
# API helpers
# --------------------------------------------------------------------------


def build_api_url(base_url: str, params: dict[str, str]) -> str:
    merged = {"format": "json", "formatversion": "2", **params}
    return f"{base_url.rstrip('/')}/w/api.php?{urlencode(merged)}"


def build_category_query(base_url: str, category: str, continue_token: str = "", limit: int = 50) -> str:
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": category if category.lower().startswith(("category:", "catégorie:")) else f"Category:{category}",
        "cmlimit": str(limit),
        "cmnamespace": "0",
    }
    if continue_token:
        params["cmcontinue"] = continue_token
    return build_api_url(base_url, params)


def build_content_query(base_url: str, titles: list[str]) -> str:
    return build_api_url(
        base_url,
        {
            "action": "query",
            "prop": "revisions|info",
            "rvprop": "content|timestamp|ids",
            "rvslots": "main",
            "titles": "|".join(titles[:20]),
            "inprop": "url",
        },
    )


# --------------------------------------------------------------------------
# Dump streaming
# --------------------------------------------------------------------------


def iter_dump_pages(xml_bytes: bytes) -> Iterator[dict]:
    """
    Yield `{title, namespace, text, page_id}` from a MediaWiki XML dump
    fragment.

    Real dumps are multi-gigabyte bzip2 streams; this parses an
    already-decompressed fragment, which is what the archive-profile
    reader feeds it chunk by chunk with a checkpoint after each. Parsing
    is via the hardened XML parser -- a dump is still untrusted input.
    """
    root = safe_parse_xml(xml_bytes)
    for page in root.iter():
        if local_name(page.tag) != "page":
            continue
        title = ""
        namespace = 0
        page_id = ""
        text = ""
        for child in page.iter():
            name = local_name(child.tag)
            if name == "title" and child.text:
                title = child.text
            elif name == "ns" and child.text and child.text.strip().lstrip("-").isdigit():
                namespace = int(child.text.strip())
            elif name == "id" and child.text and not page_id:
                page_id = child.text.strip()
            elif name == "text" and child.text:
                text = child.text
        if title:
            yield {"title": title, "namespace": namespace, "text": text, "page_id": page_id}


__all__ = [
    "CONTENT_NAMESPACES",
    "MIN_CONTENT_CHARS",
    "build_api_url",
    "build_category_query",
    "build_content_query",
    "clean_wikitext",
    "is_importable_page",
    "iter_dump_pages",
    "strip_templates",
]
