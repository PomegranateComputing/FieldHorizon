"""
Security primitives and HTTP conduct.

Covers required cases 17-22: ETag/Last-Modified, Retry-After, timeout,
maximum size, safe decompression, and MIME validation -- plus the
allowlist, redirect validation, filename neutralization, and the
guarantee that no secret reaches a log.

The HTTP tests drive `BoundedFetcher` against a stub session rather than
a live server: the behaviour under test is the fetcher's own logic
(when it retries, when it refuses, what it caps), and a real server
would make those assertions slow and flaky without making them stronger.
"""

from __future__ import annotations

import pytest
import requests

from fieldhorizon.corpus.http import (
    BoundedFetcher,
    FetchError,
    NotModified,
    RateLimiter,
    default_user_agent,
    parse_retry_after,
)
from fieldhorizon.corpus.http import _redact as redact_url
from fieldhorizon.corpus.security import (
    DisallowedURLError,
    SecurityError,
    UnsafeArchiveError,
    detect_encoding,
    ensure_within,
    host_allowed,
    html_to_text,
    open_zip,
    safe_filename,
    safe_zip_members,
    safe_zip_read,
    sniff_mime,
    validate_url,
)
from tests.corpus_helpers import build_epub, build_pdf, build_zip, fixture_bytes

# --------------------------------------------------------------- allowlist


def test_leading_dot_matches_subdomains_but_never_a_lookalike_domain():
    assert host_allowed("www.gutenberg.org", [".gutenberg.org"])
    assert host_allowed("gutenberg.org", [".gutenberg.org"])
    # The classic allowlist bypass: suffix matching without a dot boundary.
    assert not host_allowed("notgutenberg.org", [".gutenberg.org"])
    assert not host_allowed("gutenberg.org.evil.com", [".gutenberg.org"])


def test_exact_host_pattern_does_not_match_subdomains():
    assert host_allowed("archive.org", ["archive.org"])
    assert not host_allowed("evil.archive.org.attacker.net", ["archive.org"])


def test_https_is_required_unless_a_source_opts_into_http():
    validate_url("https://standardebooks.org/feed", ["standardebooks.org"])

    with pytest.raises(DisallowedURLError, match="plain HTTP"):
        validate_url("http://standardebooks.org/feed", ["standardebooks.org"])

    # Opt-in exists for the handful of institutional endpoints still
    # without TLS, and must be a deliberate per-source choice.
    validate_url("http://old-library.example.org/oai", ["old-library.example.org"], allow_http=True)


def test_non_http_schemes_are_refused():
    for url in ("file:///etc/passwd", "ftp://example.org/x", "javascript:alert(1)", "data:text/html,x"):
        with pytest.raises(DisallowedURLError):
            validate_url(url, ["example.org"])


def test_an_empty_allowlist_refuses_everything():
    """
    A source with no allowlist could fetch anywhere. That is never an
    acceptable permissive default.
    """
    with pytest.raises(DisallowedURLError, match="No host allowlist"):
        validate_url("https://example.org/x", [])


# ------------------------------------------------------- 22. MIME sniffing


def test_mime_is_sniffed_from_bytes_not_from_the_declared_content_type():
    pdf = build_pdf(["Some text"])
    result = sniff_mime(pdf, declared="text/html")
    assert result.mime == "application/pdf"
    assert result.declared_mismatch is True


def test_epub_is_distinguished_from_a_plain_zip():
    assert sniff_mime(build_epub()).mime == "application/epub+zip"
    assert sniff_mime(build_zip({"a.txt": b"hello"})).mime == "application/zip"


def test_xml_html_json_and_text_are_recognized():
    assert sniff_mime(fixture_bytes("opds_standard_ebooks.xml")).mime == "application/xml"
    assert sniff_mime(fixture_bytes("sample_page.html")).mime == "text/html"
    assert sniff_mime(b'{"a": 1}').mime == "application/json"
    assert sniff_mime(fixture_bytes("sample_book.txt")).mime == "text/plain"


@pytest.mark.parametrize(
    ("magic", "label"),
    [
        (b"MZ\x90\x00", "DOS/PE executable"),
        (b"\x7fELF\x02\x01", "ELF executable"),
        (b"\xca\xfe\xba\xbe", "Java class"),
        (b"#!/bin/sh\nrm -rf /", "shell script"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "legacy OLE (macro-bearing)"),
    ],
)
def test_executables_and_macro_bearing_formats_are_refused(magic, label):
    with pytest.raises(SecurityError):
        sniff_mime(magic + b"\x00" * 100)


def test_ooxml_is_refused_because_it_may_carry_macros():
    docx = build_zip({"word/document.xml": b"<w:document/>", "[Content_Types].xml": b"<Types/>"})
    with pytest.raises(SecurityError, match="macros"):
        sniff_mime(docx)


def test_binary_content_with_no_recognized_format_is_refused():
    with pytest.raises(SecurityError, match="binary content"):
        sniff_mime(b"\x01\x02\x00\x03\x04garbage" * 20)


def test_encoding_detection_prefers_bom_then_utf8_then_latin1():
    assert detect_encoding("﻿hello".encode("utf-8-sig")) == "utf-8-sig"
    assert detect_encoding("héllo wörld".encode()) == "utf-8"
    assert detect_encoding("héllo".encode("latin-1")) == "latin-1"
    assert detect_encoding(b"\xff\xfeh\x00i\x00") == "utf-16-le"


# --------------------------------------------------- 21. safe decompression


def test_zip_slip_member_is_refused():
    archive = open_zip(build_zip({"../../../etc/evil": b"pwned", "ok.txt": b"fine"}))
    with pytest.raises(UnsafeArchiveError, match="escapes its root"):
        safe_zip_members(archive)


@pytest.mark.parametrize("name", ["/etc/passwd", "..\\..\\windows\\system32\\evil", "C:evil.txt", "a/../../b"])
def test_unsafe_member_names_are_refused_individually(name):
    archive = open_zip(build_zip({"safe.txt": b"x"}))
    with pytest.raises(UnsafeArchiveError):
        safe_zip_read(archive, name)


def test_a_decompression_bomb_is_refused_by_its_ratio():
    archive = open_zip(build_zip({"bomb.txt": b"A" * (8 * 1024 * 1024)}))
    with pytest.raises(UnsafeArchiveError, match="decompression bomb"):
        safe_zip_members(archive)


def test_total_declared_expansion_is_capped():
    archive = open_zip(build_zip({f"part{i}.txt": b"B" * 200_000 for i in range(10)}))
    with pytest.raises(UnsafeArchiveError, match="over the"):
        safe_zip_members(archive, max_total_bytes=500_000)


def test_a_member_is_capped_during_streaming_not_from_its_header():
    """
    A zip header can lie about `file_size`. The read cap must be enforced
    on the bytes actually produced, or the ratio guard is bypassable.
    """
    archive = open_zip(build_zip({"big.txt": b"C" * 100_000}))
    with pytest.raises(UnsafeArchiveError, match="expands past"):
        safe_zip_read(archive, "big.txt", max_bytes=1000)


def test_an_epub_containing_a_zip_slip_member_is_refused_at_normalization():
    from fieldhorizon.corpus.normalization import normalize_epub

    with pytest.raises(UnsafeArchiveError):
        normalize_epub(build_epub(include_zip_slip=True))


def test_a_corrupt_archive_fails_cleanly():
    with pytest.raises(UnsafeArchiveError, match="Not a readable archive"):
        open_zip(b"PK\x03\x04 this is not really a zip file at all")


# ------------------------------------------------------------ HTML safety


def test_script_and_style_content_never_reaches_the_extracted_text():
    text = html_to_text(fixture_bytes("sample_page.html").decode("utf-8"))

    assert "attacker.example.com" not in text
    assert "if this string appears" not in text
    assert "font-family" not in text
    assert "Please enable JavaScript" not in text
    # Navigation, ads, and chrome are dropped as boilerplate.
    assert "Advertisement" not in text
    assert "Next page" not in text
    # Real content survives, with its heading structure.
    assert "government of a city" in text
    assert "Chapter I" in text


def test_html_extraction_never_follows_a_link():
    """
    The extractor is a tokenizer, not a browser: it has no concept of
    fetching, so a URL in the document is inert text at worst.
    """
    html = '<html><body><p>See <a href="https://attacker.example.com/x">here</a>.</p></body></html>'
    text = html_to_text(html)
    assert "here" in text
    assert "attacker.example.com" not in text


# --------------------------------------------------------- filename safety


@pytest.mark.parametrize(
    ("raw", "forbidden"),
    [
        ("../../etc/passwd", "/"),
        ("a/b/c.txt", "/"),
        ("evil\\..\\..\\file", "\\"),
        ("file; rm -rf /", ";"),
        ("$(whoami).txt", "$"),
        ("nul\x00byte.txt", "\x00"),
    ],
)
def test_untrusted_titles_cannot_produce_a_dangerous_filename(raw, forbidden):
    cleaned = safe_filename(raw)
    assert forbidden not in cleaned
    assert ".." not in cleaned


def test_windows_reserved_device_names_are_neutralized():
    """
    Corpora get copied to other machines; a file named CON.txt becomes
    someone else's problem.
    """
    assert safe_filename("CON.txt").startswith("_")
    assert safe_filename("lpt1.txt").startswith("_")


def test_writes_outside_the_target_directory_are_refused(tmp_path):
    base = tmp_path / "corpus"
    base.mkdir()
    ensure_within(base, base / "ok.txt")

    with pytest.raises(SecurityError, match="Refusing to write outside"):
        ensure_within(base, tmp_path / "escaped.txt")


# ------------------------------------------------- 18/19/20. HTTP conduct


class _StubResponse:
    def __init__(self, status_code=200, headers=None, body=b"", chunk_size=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self._chunk_size = chunk_size or max(1, len(body) or 1)
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for start in range(0, len(self._body), self._chunk_size):
            yield self._body[start : start + self._chunk_size]

    def close(self):
        self.closed = True


class _StubSession:
    """Records requests and replays a scripted sequence of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.headers = {}

    def get(self, url, headers=None, timeout=None, stream=False, allow_redirects=True):
        self.requests.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {url}")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _fetcher(session, **kwargs):
    fetcher = BoundedFetcher(
        default_user_agent("test@example.org"),
        requests_per_minute=0,  # no pacing delay in tests
        max_retries=kwargs.pop("max_retries", 3),
        respect_robots=False,
        **kwargs,
    )
    fetcher.session = session
    return fetcher


def test_etag_and_last_modified_are_sent_and_304_raises_not_modified():
    class _Cache:
        def get_http_cache(self, url):
            return {"etag": '"abc123"', "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT"}

        def set_http_cache(self, *args, **kwargs):
            raise AssertionError("a 304 must not overwrite the cache entry")

    session = _StubSession([_StubResponse(304)])
    fetcher = _fetcher(session)
    fetcher.cache = _Cache()

    with pytest.raises(NotModified):
        fetcher.fetch("https://example.org/feed", allowed_hosts=["example.org"], conditional=True)

    sent = session.requests[0]["headers"]
    assert sent["If-None-Match"] == '"abc123"'
    assert sent["If-Modified-Since"] == "Wed, 01 Jan 2025 00:00:00 GMT"


def test_a_successful_conditional_fetch_stores_the_new_validators():
    stored = {}

    class _Cache:
        def get_http_cache(self, url):
            return None

        def set_http_cache(self, url, etag, last_modified, status_code, content_sha256):
            stored.update(
                {"url": url, "etag": etag, "last_modified": last_modified, "status": status_code}
            )

    session = _StubSession(
        [_StubResponse(200, {"ETag": '"new"', "Last-Modified": "Thu, 02 Jan 2025 00:00:00 GMT"}, b"body text")]
    )
    fetcher = _fetcher(session)
    fetcher.cache = _Cache()

    fetcher.fetch("https://example.org/feed", allowed_hosts=["example.org"], conditional=True)
    assert stored["etag"] == '"new"'
    assert stored["status"] == 200


def test_retry_after_is_honoured_on_429(monkeypatch):
    slept = []
    monkeypatch.setattr("fieldhorizon.corpus.http.time.sleep", lambda s: slept.append(s))

    session = _StubSession(
        [_StubResponse(429, {"Retry-After": "7"}), _StubResponse(200, {}, b"finally")]
    )
    fetcher = _fetcher(session)

    response = fetcher.fetch("https://example.org/x", allowed_hosts=["example.org"])
    assert response.content == b"finally"
    # The penalty is applied through the rate limiter, which sleeps.
    assert any(abs(s - 7.0) < 0.01 for s in slept), slept


def test_retry_after_accepts_both_seconds_and_http_dates():
    assert parse_retry_after("120") == 120.0
    # An HTTP-date in the past yields 0 rather than a negative delay.
    assert parse_retry_after("Wed, 01 Jan 2020 00:00:00 GMT") == 0.0
    # An unparseable value falls back to a conservative wait, never to 0:
    # the server asking us to wait is the signal that matters.
    assert parse_retry_after("soon please") == 60.0
    assert parse_retry_after("") == 0.0


def test_a_timeout_is_retried_then_reported_as_retryable(monkeypatch):
    monkeypatch.setattr("fieldhorizon.corpus.http.time.sleep", lambda s: None)
    session = _StubSession([requests.Timeout("timed out")] * 3)
    fetcher = _fetcher(session, max_retries=3)

    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch("https://example.org/slow", allowed_hosts=["example.org"])

    assert excinfo.value.retryable is True
    assert len(session.requests) == 3


def test_the_configured_timeout_is_passed_to_every_request():
    session = _StubSession([_StubResponse(200, {}, b"ok")])
    fetcher = _fetcher(session, timeout=17.5)
    fetcher.fetch("https://example.org/x", allowed_hosts=["example.org"])
    assert session.requests[0]["timeout"] == 17.5


def test_4xx_is_final_and_not_retried(monkeypatch):
    monkeypatch.setattr("fieldhorizon.corpus.http.time.sleep", lambda s: None)
    session = _StubSession([_StubResponse(404)])
    fetcher = _fetcher(session)

    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch("https://example.org/missing", allowed_hosts=["example.org"])

    assert excinfo.value.retryable is False
    assert excinfo.value.status_code == 404
    assert len(session.requests) == 1  # never retried


def test_5xx_is_retried_and_then_reported_as_retryable(monkeypatch):
    monkeypatch.setattr("fieldhorizon.corpus.http.time.sleep", lambda s: None)
    session = _StubSession([_StubResponse(500), _StubResponse(502), _StubResponse(503, {"Retry-After": "0"})])
    fetcher = _fetcher(session, max_retries=3)

    with pytest.raises(FetchError) as excinfo:
        fetcher.fetch("https://example.org/broken", allowed_hosts=["example.org"])
    assert excinfo.value.retryable is True


def test_an_oversized_declared_body_is_refused_before_it_is_read():
    session = _StubSession([_StubResponse(200, {"Content-Length": "999999999"}, b"x")])
    fetcher = _fetcher(session)

    with pytest.raises(FetchError, match="over the"):
        fetcher.fetch("https://example.org/huge", allowed_hosts=["example.org"], max_bytes=1000)


def test_a_body_that_exceeds_the_cap_mid_stream_is_aborted():
    """
    Content-Length is a hint, not the enforcement mechanism. A server
    that omits or understates it must still not be able to make the
    harvester buffer an unbounded body.
    """
    session = _StubSession([_StubResponse(200, {}, b"y" * 50_000, chunk_size=1000)])
    fetcher = _fetcher(session)

    with pytest.raises(FetchError, match="exceeded the"):
        fetcher.fetch("https://example.org/lying", allowed_hosts=["example.org"], max_bytes=5000)


def test_a_redirect_to_a_non_allowlisted_host_is_refused():
    """
    Redirects are followed one hop at a time and re-validated each time.
    `allow_redirects=True` would hand an allowlist bypass over for free.
    """
    session = _StubSession(
        [_StubResponse(302, {"Location": "https://attacker.example.net/payload"})]
    )
    fetcher = _fetcher(session)

    with pytest.raises(FetchError, match="Refusing redirect"):
        fetcher.fetch("https://example.org/start", allowed_hosts=["example.org"])


def test_a_redirect_within_the_allowlist_is_followed():
    session = _StubSession(
        [
            _StubResponse(301, {"Location": "https://www.example.org/final"}),
            _StubResponse(200, {}, b"arrived"),
        ]
    )
    fetcher = _fetcher(session)

    response = fetcher.fetch("https://example.org/start", allowed_hosts=[".example.org"])
    assert response.content == b"arrived"
    assert response.final_url == "https://www.example.org/final"


def test_a_redirect_to_plain_http_on_a_trusted_host_is_upgraded_not_refused():
    """
    Project Gutenberg's /ebooks/<id>.rdf redirects to an `http://` URL on
    its own domain, then immediately upgrades that to https itself.
    Refusing outright made those URLs unfetchable; following the http hop
    would be a downgrade. The scheme is upgraded and re-validated, so we
    never speak plaintext and nothing is widened.
    """
    session = _StubSession(
        [
            _StubResponse(302, {"Location": "http://www.example.org/cache/thing.rdf"}),
            _StubResponse(200, {}, b"<rdf/>"),
        ]
    )
    fetcher = _fetcher(session)

    response = fetcher.fetch("https://www.example.org/thing.rdf", allowed_hosts=["www.example.org"])
    assert response.content == b"<rdf/>"
    assert response.final_url == "https://www.example.org/cache/thing.rdf"
    assert session.requests[1]["url"].startswith("https://")


def test_a_plain_http_redirect_to_an_untrusted_host_is_still_refused():
    """The upgrade must not become a way around the allowlist."""
    session = _StubSession([_StubResponse(302, {"Location": "http://attacker.example.net/payload"})])
    fetcher = _fetcher(session)

    with pytest.raises(FetchError, match="Refusing redirect"):
        fetcher.fetch("https://www.example.org/start", allowed_hosts=["www.example.org"])


def test_a_redirect_loop_terminates():
    session = _StubSession(
        [_StubResponse(302, {"Location": "https://example.org/loop"}) for _ in range(10)]
    )
    fetcher = _fetcher(session)

    with pytest.raises(FetchError, match="Too many redirects"):
        fetcher.fetch("https://example.org/loop", allowed_hosts=["example.org"])


def test_the_user_agent_carries_a_contact_address():
    agent = default_user_agent("corpus@example.org")
    assert "FieldHorizonCorpusHarvester" in agent
    assert "corpus@example.org" in agent
    # An unset contact is visible rather than silently blank.
    assert "unconfigured-contact" in default_user_agent("")


def test_no_credential_ever_reaches_a_log():
    for url, secret in [
        ("https://api.europeana.eu/search?wskey=SUPERSECRET&query=x", "SUPERSECRET"),
        ("https://example.org/x?api_key=abc123", "abc123"),
        ("https://example.org/x?access_token=hunter2value&b=2", "hunter2value"),
    ]:
        redacted = redact_url(url)
        assert secret not in redacted
        assert "REDACTED" in redacted


def test_the_request_log_records_urls_without_headers():
    session = _StubSession([_StubResponse(200, {"Authorization": "Bearer secret"}, b"ok")])
    fetcher = _fetcher(session)
    fetcher.fetch("https://example.org/x?token=hunter2", allowed_hosts=["example.org"])

    entry = fetcher.request_log[0]
    assert "hunter2" not in entry["url"]
    assert "headers" not in entry


def test_rate_limiter_paces_requests_per_host(monkeypatch):
    slept = []
    monkeypatch.setattr("fieldhorizon.corpus.http.time.sleep", lambda s: slept.append(s))

    limiter = RateLimiter(requests_per_minute=60)  # one per second
    limiter.wait("example.org")
    limiter.wait("example.org")

    assert slept, "the second request to the same host should have been paced"


def test_rate_limiter_paces_hosts_independently(monkeypatch):
    slept = []
    monkeypatch.setattr("fieldhorizon.corpus.http.time.sleep", lambda s: slept.append(s))

    limiter = RateLimiter(requests_per_minute=60)
    limiter.wait("a.example.org")
    limiter.wait("b.example.org")

    # Different hosts share no budget: one slow server must not throttle
    # every other source in the run.
    assert not slept
