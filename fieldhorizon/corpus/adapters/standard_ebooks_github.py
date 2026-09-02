"""
Standard Ebooks, via its official public GitHub repositories.

Phase I found that `/feeds/opds/all` now returns **401**: the bulk
catalogue requires an account. We do not have credentials and do not
bypass authentication, so Phase I fell back to the public new-releases
feed — official and unauthenticated, but only the fifteen most recent
releases.

Standard Ebooks also publishes every ebook as a public GitHub repository.
That is a genuinely open, documented route to the full catalogue, and
using it is not a workaround: it is a different published interface, used
as published, with no gate to step around.

**The rights case here is the mandatory lattice case, verbatim.** A real
`dc:rights` field states both layers in one string:

> The source text and artwork in this ebook are believed to be in the
> **United States public domain** … users located outside of the United
> States must check their local laws before using this ebook. **The
> creators of, and contributors to, this ebook dedicate their
> contributions** to the worldwide public domain via … CC0 1.0.

Phase I's single-licence engine matched CC0 first and produced a
worldwide grant. Here the two clauses are split into `SOURCE_TEXT` and
`EDITORIAL_CONTRIBUTION` and intersected, so the result is
`LOCAL_US_ONLY` — structurally, not by pattern ordering.

**No EPUB is built.** The repository's `src/epub/text/*.xhtml` is the
source of the text, and the spine order is in `content.opf`. Zipping
those into an EPUB only to unzip it again would add a step that can fail
and no information.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import quote

from ..capabilities import Capability, SourceCapabilities
from ..language import normalize_language_code
from ..lattice import RightsComponent, RightsStack
from ..models import Candidate, Evidence, RightsSignal
from ..rights import evidence_from_field
from ..security import child_local, findall_local, html_to_text, local_name, safe_parse_xml
from .base import DiscoveryResult, SourceAdapter, register_adapter

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_ORG = "standardebooks"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"

#: Repositories that are tooling, infrastructure, or documentation rather
#: than ebooks. Standard Ebooks' organisation contains a handful of them
#: and they must not be harvested as literature.
_NON_EBOOK_REPOS = frozenset(
    {
        "tools", "manual", "web", "standardebooks.org", "css", "highlight-rules",
        "smart-quotes", "word-count", ".github", "docs", "roadmap", "shell-completion",
    }
)

#: An ebook repository is named `author_title` or `author_title_translator`
#: -- lowercase, underscore-separated, at least two parts. Tooling repos
#: are single words. Matching the convention is more robust than an
#: exclusion list alone, which would silently start harvesting the next
#: tool somebody adds.
_EBOOK_REPO_RE = re.compile(r"^[a-z0-9][a-z0-9-]*_[a-z0-9][a-z0-9-]*(_[a-z0-9][a-z0-9-]*)*$")

STANDARD_EBOOKS_GITHUB_CAPABILITIES = SourceCapabilities(
    provider_id="standard_ebooks_github",
    capabilities=frozenset(
        {
            Capability.DISCOVERY,
            Capability.METADATA,
            Capability.RIGHTS_EVIDENCE,
            Capability.CONTENT_HOSTING,
            Capability.RENDERED_TEXT,
            Capability.BULK_SNAPSHOT,
            Capability.INCREMENTAL_UPDATES,
            Capability.SUPPORTS_RESUME,
        }
    ),
    credential_env=GITHUB_TOKEN_ENV,
    rights_trust=0.95,
    content_hosts=("api.github.com", "raw.githubusercontent.com", "github.com"),
    notes=(
        "Public repositories, used as published. A $GITHUB_TOKEN raises the rate limit "
        "from 60 to 5000 requests/hour but is not required; without one the adapter "
        "simply harvests more slowly."
    ),
)


def is_ebook_repository(name: str) -> bool:
    """
    Whether a repository holds an ebook rather than tooling.

    Both tests apply: the name must match the `author_title` convention
    AND not be on the exclusion list. The convention alone would be
    fooled by a hyphenated tool name; the list alone would silently start
    harvesting the next tool somebody adds.
    """
    lowered = name.lower()
    if lowered in _NON_EBOOK_REPOS:
        return False
    return bool(_EBOOK_REPO_RE.match(lowered))


def github_headers(token: str = "") -> dict[str, str]:
    """
    Request headers. The token, when present, comes from the environment
    and is never logged -- `http._redact` strips credential-bearing query
    parameters, and this one travels in a header that the request log
    does not record at all.
    """
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_repository_list(payload: bytes) -> list[dict]:
    """
    The org's repository listing, filtered to ebooks.

    `pushed_at` and the default branch are kept: the first is the
    incremental signal, the second is needed to address raw files.
    """
    data = json.loads(payload.decode("utf-8", errors="replace"))
    if isinstance(data, dict):
        # An error body ({"message": "API rate limit exceeded..."}).
        raise GitHubAPIError(str(data.get("message", data))[:200])

    repositories = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        if not is_ebook_repository(name):
            continue
        repositories.append(
            {
                "name": name,
                "full_name": entry.get("full_name", f"{GITHUB_ORG}/{name}"),
                "default_branch": entry.get("default_branch", "master"),
                "pushed_at": entry.get("pushed_at", ""),
                "updated_at": entry.get("updated_at", ""),
                "size": int(entry.get("size", 0) or 0),
                "html_url": entry.get("html_url", ""),
            }
        )
    return repositories


class GitHubAPIError(RuntimeError):
    """The API returned an error body -- usually a rate limit."""


def raw_url(full_name: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{full_name}/{quote(branch)}/{path}"


# --------------------------------------------------------------------------
# OPF
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EbookMetadata:
    title: str
    authors: tuple[str, ...]
    translators: tuple[str, ...]
    language: str
    rights_text: str
    subjects: tuple[str, ...]
    sources: tuple[str, ...]
    description: str
    #: Reading order: `src/epub/text/...` paths from the spine.
    spine: tuple[str, ...]


def parse_content_opf(xml_bytes: bytes) -> EbookMetadata:
    """
    Standard Ebooks' `content.opf`.

    The spine is resolved to actual file paths through the manifest,
    because the spine references manifest ids rather than hrefs. Reading
    the manifest in file order instead would give alphabetical chapters,
    which is both wrong and hard to notice from a quality score.

    Roles are read from `opf:role` refinements: `aut` is an author, `trl`
    a translator. The distinction matters downstream -- the deduplicator
    never merges two translations, and it can only honour that if the
    translator is recorded.
    """
    root = safe_parse_xml(xml_bytes)

    metadata_el = child_local(root, "metadata")
    manifest_el = child_local(root, "manifest")
    spine_el = child_local(root, "spine")

    def _values(name: str) -> list[str]:
        if metadata_el is None:
            return []
        out = []
        for element in metadata_el:
            if local_name(element.tag).lower() == name and element.text:
                value = " ".join(element.text.split())
                if value:
                    out.append(value)
        return out

    # Creator roles live in <meta refines="#id" property="role">, and one
    # person carries SEVERAL of them: the real fixture gives the
    # translator `ann`, `trl`, and `wpr` in that order. Keeping only the
    # last would have made Aylmer Maude a "writer of preface" and dropped
    # the translation layer from the rights stack entirely.
    roles: dict[str, set[str]] = {}
    if metadata_el is not None:
        for element in metadata_el:
            if local_name(element.tag).lower() != "meta":
                continue
            if element.attrib.get("property") != "role":
                continue
            refines = (element.attrib.get("refines") or "").lstrip("#")
            if refines and element.text:
                roles.setdefault(refines, set()).add(element.text.strip().lower())

    authors: list[str] = []
    translators: list[str] = []
    if metadata_el is not None:
        for element in metadata_el:
            name = local_name(element.tag).lower()
            if name not in ("creator", "contributor") or not element.text:
                continue
            person = " ".join(element.text.split())
            person_roles = roles.get(element.attrib.get("id", ""), set())
            if "trl" in person_roles:
                translators.append(person)
            elif name == "creator" or "aut" in person_roles:
                authors.append(person)

    manifest: dict[str, str] = {}
    if manifest_el is not None:
        for item in findall_local(manifest_el, "item"):
            item_id = item.attrib.get("id", "")
            href = item.attrib.get("href", "")
            if item_id and href:
                manifest[item_id] = href

    spine: list[str] = []
    if spine_el is not None:
        for itemref in findall_local(spine_el, "itemref"):
            spine_href = manifest.get(itemref.attrib.get("idref", ""))
            if spine_href:
                spine.append(spine_href)

    return EbookMetadata(
        title=(_values("title") or [""])[0],
        authors=tuple(authors),
        translators=tuple(translators),
        language=normalize_language_code((_values("language") or [""])[0]),
        rights_text=(_values("rights") or [""])[0],
        subjects=tuple(_values("subject")[:24]),
        sources=tuple(_values("source")),
        description=(_values("description") or [""])[0][:2000],
        spine=tuple(spine),
    )


# --------------------------------------------------------------------------
# Rights
# --------------------------------------------------------------------------

#: The US-jurisdiction clause. Standard Ebooks words it as "believed to
#: be in the United States public domain", which the Phase I normalizer
#: does not match on its own -- "believed to be in the" sits between the
#: phrase and "public domain in the United States" that it looks for.
_US_PD_CLAUSE = re.compile(
    r"(?:believed to be in the |in the )?united states public domain|"
    r"public domain in the united states",
    re.IGNORECASE,
)
_CHECK_LOCAL_LAWS = re.compile(
    r"outside of the united states must check their local laws|"
    r"may still be copyrighted in other countries",
    re.IGNORECASE,
)
_CC0_CONTRIBUTION = re.compile(
    r"(?:creators of,? and contributors to,? this ebook |contributors? )?"
    r"dedicate (?:their contributions|this)[^.]*?cc0|"
    r"cc0 1\.0 universal public domain dedication",
    re.IGNORECASE,
)


def split_standard_ebooks_rights(rights_text: str) -> tuple[str, str]:
    """
    Separate the two clauses Standard Ebooks packs into one field.

    Returns (source_text_licence, editorial_contribution_licence).

    This is the whole reason the lattice exists. The single field says
    the WORK is US public domain and, separately, that the EDITION's
    contributions are CC0. Reading it as one licence -- as Phase I did --
    produces whichever clause a regex happens to reach first, and Phase I
    reached CC0.
    """
    text = " ".join((rights_text or "").split())
    if not text:
        return "", ""

    source_text = ""
    if _US_PD_CLAUSE.search(text) or _CHECK_LOCAL_LAWS.search(text):
        # Normalized to the phrase the licence vocabulary recognises. The
        # provider's own wording is preserved as evidence, unmodified.
        source_text = "Public domain in the United States"
    elif "public domain" in text.lower():
        source_text = "This work is in the public domain."

    editorial = "CC0 1.0 Universal" if _CC0_CONTRIBUTION.search(text) else ""
    return source_text, editorial


def rights_stack_for(metadata: EbookMetadata, *, license_text: str, repo_url: str) -> RightsStack:
    """
    Two layers, stated separately, intersected by the lattice.

    `LICENSE.md` is read as corroborating evidence rather than as a
    second opinion: it carries the same two clauses, and recording it
    means a future divergence between the OPF and the licence file is
    visible rather than silently resolved.
    """
    source_licence, editorial_licence = split_standard_ebooks_rights(metadata.rights_text)

    evidence: tuple[Evidence, ...] = (
        evidence_from_field("standard_ebooks:opf:dc:rights", metadata.rights_text, repo_url),
    )
    if license_text:
        evidence += (
            evidence_from_field("standard_ebooks:LICENSE.md", license_text[:2000], repo_url),
        )

    stack = RightsStack()
    stack.add(
        RightsComponent.SOURCE_TEXT,
        source_licence,
        evidence=evidence,
        notes="the underlying work, as stated in the ebook's own rights field",
    )
    if editorial_licence:
        stack.add(
            RightsComponent.EDITORIAL_CONTRIBUTION,
            editorial_licence,
            evidence=evidence,
            notes="Standard Ebooks' typesetting, proofreading, and markup only",
        )
    if metadata.translators:
        # A translation carries its own copyright. Standard Ebooks only
        # publishes translations it believes to be public domain, and
        # says so in the same field -- but the layer is recorded so the
        # deduplicator and any future audit can see it.
        stack.add(
            RightsComponent.TRANSLATION,
            source_licence,
            evidence=evidence,
            notes=f"translation by {', '.join(metadata.translators)}",
        )
    return stack


# --------------------------------------------------------------------------
# Text assembly
# --------------------------------------------------------------------------

#: Spine items that describe the edition rather than carrying the work.
#: Same exclusion the EPUB normalizer applies, for the same reason: every
#: Standard Ebooks text otherwise ends with several hundred words about
#: copyright law.
_APPARATUS = ("colophon", "uncopyright", "imprint", "titlepage", "halftitlepage")


def is_apparatus(href: str) -> bool:
    return any(marker in href.lower() for marker in _APPARATUS)


def assemble_text(sections: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """
    Join spine sections in reading order.

    Returns (text, apparatus_texts). Apparatus is separated rather than
    dropped: it is the best licence evidence the repository contains, and
    it belongs in the licence artifact rather than in the corpus.
    """
    body: list[str] = []
    apparatus: list[str] = []
    for href, xhtml in sections:
        text = html_to_text(xhtml).strip()
        if not text:
            continue
        if is_apparatus(href):
            apparatus.append(text)
        else:
            body.append(text)
    return "\n\n".join(body), apparatus


@register_adapter
class StandardEbooksGitHubAdapter(SourceAdapter):
    """
    Options:
      `org`         -- GitHub organisation (default "standardebooks")
      `per_page`    -- repositories per API page
      `max_repos`   -- ceiling on repositories examined per run
      `token_env`   -- environment variable holding a GitHub token
    """

    adapter_name = "standard_ebooks_github"
    capabilities = STANDARD_EBOOKS_GITHUB_CAPABILITIES

    def token(self) -> str:
        return os.environ.get(self.config.option("token_env", GITHUB_TOKEN_ENV), "").strip()

    def _api(self, path: str, *, conditional: bool = True):
        token = self.token()
        headers = github_headers(token)
        # The fetcher takes an Accept string; the auth header is applied
        # to the session so it never reaches the per-request log.
        if token and hasattr(self.fetcher, "session"):
            self.fetcher.session.headers.update({"Authorization": headers["Authorization"]})
        return self.fetch_metadata(
            f"{GITHUB_API}{path}", conditional=conditional, accept=headers["Accept"]
        )

    def discover_page(self, cursor: str | None) -> DiscoveryResult:
        page = int(cursor) if cursor and cursor.isdigit() else 1
        per_page = int(self.config.option("per_page", 30))
        org = self.config.option("org", GITHUB_ORG)

        response = self._api(f"/orgs/{org}/repos?per_page={per_page}&page={page}&sort=updated")
        repositories = parse_repository_list(response.content)

        candidates: list[Candidate] = []
        for repo in repositories:
            candidate = self._candidate_for(repo)
            if candidate is not None:
                candidates.append(candidate)

        # An empty page means the listing is exhausted. GitHub does not
        # report a total, so a short page is the only end signal.
        exhausted = not repositories
        return DiscoveryResult(
            candidates=candidates,
            next_cursor=None if exhausted else str(page + 1),
            exhausted=exhausted,
        )

    def _candidate_for(self, repo: dict) -> Candidate | None:
        """
        Read one repository's OPF and licence to build a candidate.

        `pushed_at` becomes the edition date and the incremental key: a
        repository whose push timestamp has not moved has not changed,
        so a later run recognises it without re-reading anything.
        """
        full_name = repo["full_name"]
        branch = repo["default_branch"]
        opf_url = raw_url(full_name, branch, "src/epub/content.opf")

        try:
            opf = self.fetcher.fetch(
                opf_url,
                allowed_hosts=self.config.allowed_hosts,
                max_bytes=self.config.max_download_bytes,
                conditional=True,
            )
        except Exception as exc:
            logger.info("%s: no readable content.opf (%s)", full_name, exc)
            return None

        try:
            metadata = parse_content_opf(opf.content)
        except Exception as exc:
            logger.info("%s: unparseable content.opf (%s)", full_name, exc)
            return None

        license_text = ""
        try:
            license_response = self.fetcher.fetch(
                raw_url(full_name, branch, "LICENSE.md"),
                allowed_hosts=self.config.allowed_hosts,
                max_bytes=self.config.max_download_bytes,
                conditional=True,
            )
            license_text = license_response.content.decode("utf-8", errors="replace")
        except Exception:
            # Absent LICENSE.md is not fatal: the OPF's own rights field
            # is the authoritative statement, and the file corroborates.
            pass

        stack = rights_stack_for(metadata, license_text=license_text, repo_url=repo["html_url"])
        source_text = stack.get(RightsComponent.SOURCE_TEXT)

        return Candidate(
            source_id=self.config.source_id,
            external_id=repo["name"],
            title=metadata.title,
            authors=metadata.authors,
            contributors=metadata.translators,
            translator=", ".join(metadata.translators),
            language=metadata.language or "en",
            edition_date=repo.get("pushed_at", ""),
            document_type="ebook",
            subjects=metadata.subjects,
            canonical_url=repo["html_url"],
            # The spine is fetched at content time, not now: discovery
            # never downloads content.
            download_url=raw_url(full_name, branch, "src/epub/content.opf"),
            download_format="xhtml",
            estimated_bytes=repo.get("size", 0) * 1024,
            work_identifiers=tuple(f"se:{s}" for s in metadata.sources),
            rights=RightsSignal(
                content_license=source_text.raw_license if source_text else "",
                provider_declared_scope="",
                evidence=source_text.evidence if source_text else (),
                raw_rights_text=metadata.rights_text,
            ),
            raw_metadata={
                "repo": full_name,
                "branch": branch,
                "pushed_at": repo.get("pushed_at", ""),
                "spine": list(metadata.spine),
                "sources": list(metadata.sources),
                "rights_components": stack.to_dict(),
                "description": metadata.description,
            },
        )

    def fetch_content(self, candidate: Candidate, max_bytes: int = 0):
        """
        Fetch the spine's XHTML and assemble it in reading order.

        No EPUB is built. The repository's `src/epub/text/*.xhtml` IS the
        text and `content.opf` already gives the order; zipping them into
        an EPUB only to unzip it again would add a step that can fail and
        no information at all.
        """
        from ..http import FetchResponse
        from ..security import sha256_bytes

        meta = candidate.raw_metadata or {}
        full_name = meta.get("repo", "")
        branch = meta.get("branch", "master")
        spine = meta.get("spine") or []
        if not full_name or not spine:
            raise ValueError(f"{candidate.external_id}: no spine recorded during discovery")

        sections: list[tuple[str, str]] = []
        for href in spine:
            path = href if href.startswith("src/") else f"src/epub/{href.lstrip('/')}"
            try:
                response = self.fetcher.fetch(
                    raw_url(full_name, branch, path),
                    allowed_hosts=self.config.allowed_hosts,
                    max_bytes=max_bytes or self.config.max_download_bytes,
                )
            except Exception as exc:
                logger.info("%s: spine item %r unreadable (%s)", full_name, path, exc)
                continue
            sections.append((href, response.content.decode("utf-8", errors="replace")))

        if not sections:
            raise ValueError(f"{candidate.external_id}: no spine item could be read")

        text, apparatus = assemble_text(sections)
        if not text.strip():
            raise ValueError(f"{candidate.external_id}: spine produced no body text")

        payload = text.encode("utf-8")
        return FetchResponse(
            url=candidate.download_url,
            final_url=candidate.canonical_url,
            status_code=200,
            content=payload,
            headers={},
            detected_mime="text/plain",
            declared_mime="text/plain",
            encoding="utf-8",
            sha256=sha256_bytes(payload),
        )


__all__ = [
    "GITHUB_TOKEN_ENV",
    "STANDARD_EBOOKS_GITHUB_CAPABILITIES",
    "EbookMetadata",
    "GitHubAPIError",
    "StandardEbooksGitHubAdapter",
    "assemble_text",
    "is_apparatus",
    "is_ebook_repository",
    "parse_content_opf",
    "parse_repository_list",
    "raw_url",
    "rights_stack_for",
    "split_standard_ebooks_rights",
]
