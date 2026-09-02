"""
Standard Ebooks via its public GitHub repositories.

Phase I found `/feeds/opds/all` returning 401 and fell back to the
fifteen-item new-releases feed rather than bypass the authentication.
The repositories are a different published interface, used as published,
with no gate to step around — so this is a route to the full catalogue
rather than a workaround.

The fixtures are the live service's own responses, captured 2026-08-01.
The `dc:rights` field in particular is verbatim, because it is the
mandatory lattice case appearing in real data: one string stating that
the WORK is US public domain and, separately, that the EDITION's
contributions are CC0.
"""

from __future__ import annotations

import json

import pytest

from fieldhorizon.corpus.adapters.standard_ebooks_github import (
    STANDARD_EBOOKS_GITHUB_CAPABILITIES,
    GitHubAPIError,
    StandardEbooksGitHubAdapter,
    assemble_text,
    is_apparatus,
    is_ebook_repository,
    parse_content_opf,
    parse_repository_list,
    raw_url,
    rights_stack_for,
    split_standard_ebooks_rights,
)
from fieldhorizon.corpus.capabilities import Capability
from fieldhorizon.corpus.lattice import RightsComponent, decide, intersect
from fieldhorizon.corpus.models import Decision, DistributionScope
from fieldhorizon.corpus.rights import LIC_PUBLIC_DOMAIN_US, get_profile
from tests.corpus_helpers import FIXTURES, fixture_bytes, fixture_text, make_fetcher, make_source

US = get_profile("local_research_us")
WORLD = get_profile("release_worldwide")

REPOS_URL = "https://api.github.com/orgs/standardebooks/repos?per_page=30&page=1&sort=updated"
REPO = "standardebooks/leo-tolstoy_hadji-murad_aylmer-maude"


def source(**options):
    return make_source(
        "standard_ebooks_github", "standard_ebooks_github",
        base_url="https://api.github.com",
        allowed_hosts=["api.github.com", "raw.githubusercontent.com", "github.com"],
        options=options,
    )


# ------------------------------------------------- repository recognition


def test_ebook_repositories_are_told_apart_from_tooling():
    """
    The organisation contains tooling alongside its ebooks. Harvesting
    `tools` as literature would be absurd, and the live listing really
    does include it.
    """
    assert is_ebook_repository("leo-tolstoy_hadji-murad_aylmer-maude")
    assert is_ebook_repository("charles-kingsley_hypatia")
    assert is_ebook_repository("josiah-henson_father-hensons-story-of-his-own-life")

    assert not is_ebook_repository("tools")
    assert not is_ebook_repository("manual")
    assert not is_ebook_repository("web")
    assert not is_ebook_repository(".github")


def test_the_live_repository_listing_filters_out_tooling():
    repositories = parse_repository_list(fixture_bytes("se_github_repos.json"))
    names = {r["name"] for r in repositories}

    assert "tools" not in names, "the live listing contains 'tools' and it must be excluded"
    assert "leo-tolstoy_hadji-murad_aylmer-maude" in names
    assert all("_" in name for name in names)


def test_the_listing_keeps_the_incremental_signal():
    """
    `pushed_at` is how a later run knows a repository has not changed,
    which is what makes the harvest incremental rather than a full
    re-read every time.
    """
    for repo in parse_repository_list(fixture_bytes("se_github_repos.json")):
        assert repo["pushed_at"]
        assert repo["default_branch"]
        assert repo["full_name"].startswith("standardebooks/")


def test_a_rate_limit_body_raises_rather_than_parsing_as_zero_repositories():
    """
    GitHub answers a rate limit with a JSON object, not a list. Treating
    it as an empty listing would report "discovery complete, 0 found" --
    a silent, total failure dressed as success.
    """
    body = json.dumps({"message": "API rate limit exceeded for 1.2.3.4", "documentation_url": "..."})
    with pytest.raises(GitHubAPIError, match="rate limit"):
        parse_repository_list(body.encode())


# ------------------------------------------------------------------- OPF


def test_the_live_content_opf_parses():
    metadata = parse_content_opf(fixture_bytes("se_github_content.opf"))

    assert metadata.title == "Hadji Murád"
    assert "Leo Tolstoy" in " ".join(metadata.authors)
    assert metadata.language == "en"
    assert metadata.rights_text


def test_the_translator_is_recorded_separately_from_the_author():
    """
    The deduplicator never merges two translations, and it can only
    honour that if the translator is recorded as a translator rather than
    flattened into the author list.
    """
    metadata = parse_content_opf(fixture_bytes("se_github_content.opf"))

    assert metadata.translators, "this edition is a translation and says so"
    assert "Maude" in " ".join(metadata.translators)
    assert not any("Maude" in author for author in metadata.authors)


def test_the_spine_resolves_through_the_manifest_into_reading_order():
    """
    The spine references manifest ids, not hrefs. Reading the manifest in
    file order instead would give alphabetical chapters -- wrong, and
    hard to notice from a quality score.
    """
    metadata = parse_content_opf(fixture_bytes("se_github_content.opf"))

    assert metadata.spine
    assert all(href.endswith(".xhtml") for href in metadata.spine)
    chapters = [h for h in metadata.spine if "chapter" in h]
    assert chapters == sorted(chapters, key=lambda h: int("".join(c for c in h if c.isdigit()) or 0))


def test_source_identifiers_are_kept_for_cross_source_deduplication():
    metadata = parse_content_opf(fixture_bytes("se_github_content.opf"))
    assert metadata.sources
    assert any("wikisource" in s.lower() or "gutenberg" in s.lower() or "books.google" in s.lower()
               for s in metadata.sources)


# ------------------------------------------ the mandatory lattice case


def test_the_single_rights_field_splits_into_two_layers():
    """
    **The mandatory case, in real data.** One string states that the WORK
    is US public domain and, separately, that the EDITION's contributions
    are CC0. Phase I's single-licence engine reached CC0 first and
    produced a worldwide grant.
    """
    metadata = parse_content_opf(fixture_bytes("se_github_content.opf"))
    source_licence, editorial_licence = split_standard_ebooks_rights(metadata.rights_text)

    assert "United States" in source_licence
    assert "CC0" in editorial_licence


def test_the_real_ebook_resolves_to_local_us_only():
    """
    The entire point of the rights lattice, asserted against the live
    service's own words.
    """
    metadata = parse_content_opf(fixture_bytes("se_github_content.opf"))
    stack = rights_stack_for(
        metadata, license_text=fixture_text("se_github_LICENSE.md"),
        repo_url=f"https://github.com/{REPO}",
    )
    result = intersect(stack)

    assert result.scope == DistributionScope.LOCAL_US_ONLY
    assert result.effective_license == LIC_PUBLIC_DOMAIN_US
    assert RightsComponent.SOURCE_TEXT in result.limiting_components

    assert decide(stack, US)[0] == Decision.ACCEPT
    assert decide(stack, WORLD)[0] == Decision.QUARANTINE


def test_the_cc0_layer_is_recorded_as_covering_the_edition_only():
    metadata = parse_content_opf(fixture_bytes("se_github_content.opf"))
    stack = rights_stack_for(metadata, license_text="", repo_url="https://github.com/x")

    editorial = stack.get(RightsComponent.EDITORIAL_CONTRIBUTION)
    assert editorial is not None
    assert "typesetting" in editorial.notes
    assert "markup" in editorial.notes


def test_the_licence_file_is_recorded_as_corroborating_evidence():
    """
    Recorded rather than merged, so a future divergence between the OPF
    and LICENSE.md is visible instead of silently resolved.
    """
    metadata = parse_content_opf(fixture_bytes("se_github_content.opf"))
    stack = rights_stack_for(
        metadata, license_text=fixture_text("se_github_LICENSE.md"),
        repo_url=f"https://github.com/{REPO}",
    )
    kinds = {e.kind for e in stack.get(RightsComponent.SOURCE_TEXT).evidence}

    assert "standard_ebooks:opf:dc:rights" in kinds
    assert "standard_ebooks:LICENSE.md" in kinds


def test_a_translation_layer_is_recorded_when_there_is_a_translator():
    metadata = parse_content_opf(fixture_bytes("se_github_content.opf"))
    stack = rights_stack_for(metadata, license_text="", repo_url="https://github.com/x")

    translation = stack.get(RightsComponent.TRANSLATION)
    assert translation is not None
    assert "Maude" in translation.notes


@pytest.mark.parametrize(
    ("text", "expect_source", "expect_editorial"),
    [
        ("The source text ... believed to be in the United States public domain ... "
         "dedicate their contributions ... CC0 1.0 Universal Public Domain Dedication.",
         "United States", "CC0"),
        ("This work is in the public domain worldwide.", "public domain", ""),
        ("", "", ""),
    ],
)
def test_rights_splitting_across_wordings(text, expect_source, expect_editorial):
    source_licence, editorial_licence = split_standard_ebooks_rights(text)
    assert expect_source in source_licence
    assert expect_editorial in editorial_licence


# ------------------------------------------------------- text assembly


def test_the_spine_is_assembled_and_apparatus_is_separated():
    """
    Colophon and uncopyright pages are the best licence evidence the
    repository contains, and they belong in the licence artifact rather
    than in the corpus -- otherwise every text ends with several hundred
    words about copyright law.
    """
    sections = [
        ("chapter-1.xhtml", fixture_text("se_github_chapter.xhtml")),
        ("colophon.xhtml", fixture_text("se_github_colophon.xhtml")),
    ]
    text, apparatus = assemble_text(sections)

    assert "returning home by the fields" in text
    assert "Colophon" not in text
    assert "produced for Standard Ebooks" not in text

    assert apparatus
    assert "produced for Standard Ebooks" in apparatus[0]


def test_apparatus_is_recognised_by_filename():
    for href in ("colophon.xhtml", "uncopyright.xhtml", "imprint.xhtml", "titlepage.xhtml"):
        assert is_apparatus(href)
    for href in ("chapter-1.xhtml", "preface.xhtml", "epilogue.xhtml"):
        assert not is_apparatus(href)


def test_no_epub_is_built_the_xhtml_is_the_text():
    """
    The repository's XHTML is the source and `content.opf` already gives
    the order. Zipping them into an EPUB only to unzip it again would add
    a step that can fail and no information.
    """
    import inspect

    from fieldhorizon.corpus.adapters import standard_ebooks_github

    source_code = inspect.getsource(standard_ebooks_github)
    assert "zipfile" not in source_code
    assert "ZipFile" not in source_code


# ------------------------------------------------------------- adapter


def test_discovery_reads_repositories_opf_and_licence_offline():
    fetcher = make_fetcher(
        {
            REPOS_URL: FIXTURES / "se_github_repos.json",
            raw_url(REPO, "master", "src/epub/content.opf"): FIXTURES / "se_github_content.opf",
            raw_url(REPO, "master", "LICENSE.md"): FIXTURES / "se_github_LICENSE.md",
        }
    )
    adapter = StandardEbooksGitHubAdapter(source(per_page=30), fetcher)
    result = adapter.discover_page(None)

    tolstoy = next(c for c in result.candidates if "hadji" in c.external_id)
    assert tolstoy.title == "Hadji Murád"
    assert tolstoy.translator
    assert tolstoy.raw_metadata["spine"]
    assert tolstoy.raw_metadata["rights_components"]["source_text"]["normalized_license"] == (
        LIC_PUBLIC_DOMAIN_US
    )


def test_a_repository_whose_opf_is_missing_is_skipped_not_fatal():
    """
    One unreadable repository must not abort the listing -- the same
    per-item containment the rest of the harvester uses.
    """
    fetcher = make_fetcher({REPOS_URL: FIXTURES / "se_github_repos.json"})
    result = StandardEbooksGitHubAdapter(source(per_page=30), fetcher).discover_page(None)
    assert result.candidates == []


def test_content_is_assembled_from_the_spine():
    chapter = fixture_text("se_github_chapter.xhtml")
    colophon = fixture_text("se_github_colophon.xhtml")

    fetcher = make_fetcher(
        {
            REPOS_URL: FIXTURES / "se_github_repos.json",
            raw_url(REPO, "master", "src/epub/content.opf"): FIXTURES / "se_github_content.opf",
            raw_url(REPO, "master", "LICENSE.md"): FIXTURES / "se_github_LICENSE.md",
        }
    )
    adapter = StandardEbooksGitHubAdapter(source(per_page=30), fetcher)
    candidate = next(
        c for c in adapter.discover_page(None).candidates if "hadji" in c.external_id
    )

    # Serve every spine item the OPF named.
    for href in candidate.raw_metadata["spine"]:
        path = href if href.startswith("src/") else f"src/epub/{href.lstrip('/')}"
        body = colophon if "colophon" in href else chapter
        fetcher.mapping[raw_url(REPO, "master", path)] = body.encode("utf-8")

    response = adapter.fetch_content(candidate)
    text = response.content.decode("utf-8")

    assert "returning home by the fields" in text
    assert "produced for Standard Ebooks" not in text
    assert response.detected_mime == "text/plain"


def test_a_spine_that_yields_nothing_is_an_explicit_error():
    fetcher = make_fetcher(
        {
            REPOS_URL: FIXTURES / "se_github_repos.json",
            raw_url(REPO, "master", "src/epub/content.opf"): FIXTURES / "se_github_content.opf",
            raw_url(REPO, "master", "LICENSE.md"): FIXTURES / "se_github_LICENSE.md",
        }
    )
    adapter = StandardEbooksGitHubAdapter(source(per_page=30), fetcher)
    candidate = next(
        c for c in adapter.discover_page(None).candidates if "hadji" in c.external_id
    )

    with pytest.raises(ValueError, match="no spine item could be read"):
        adapter.fetch_content(candidate)


# -------------------------------------------------------- capabilities


def test_the_adapter_declares_what_it_does_including_bulk_and_resume():
    caps = STANDARD_EBOOKS_GITHUB_CAPABILITIES
    for capability in (Capability.DISCOVERY, Capability.METADATA, Capability.RIGHTS_EVIDENCE,
                       Capability.CONTENT_HOSTING, Capability.RENDERED_TEXT,
                       Capability.BULK_SNAPSHOT, Capability.INCREMENTAL_UPDATES,
                       Capability.SUPPORTS_RESUME):
        assert caps.has(capability), capability


def test_a_github_token_is_optional_not_required(monkeypatch):
    """
    A token raises the rate limit from 60 to 5000 requests an hour. Its
    absence must slow the harvest, never stop it.
    """
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    adapter = StandardEbooksGitHubAdapter(source(), make_fetcher({}))

    assert adapter.token() == ""
    # No credentialled capabilities: everything works unauthenticated.
    assert STANDARD_EBOOKS_GITHUB_CAPABILITIES.credentialled_capabilities == frozenset()
    assert STANDARD_EBOOKS_GITHUB_CAPABILITIES.credential_env == "GITHUB_TOKEN"


def test_the_token_never_appears_in_the_capability_record():
    payload = STANDARD_EBOOKS_GITHUB_CAPABILITIES.to_dict()
    assert payload["credential_env"] == "GITHUB_TOKEN"
    assert "Bearer" not in str(payload)


def test_the_adapter_makes_no_network_call_in_tests():
    from fieldhorizon.corpus.http import FetchError

    with pytest.raises(FetchError, match="no fixture"):
        StandardEbooksGitHubAdapter(source(), make_fetcher({})).discover_page(None)
