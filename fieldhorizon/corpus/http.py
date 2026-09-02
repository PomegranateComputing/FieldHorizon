"""
The only way the harvester reaches the network.

Every request made by every adapter goes through `BoundedFetcher`. That
concentration is the point: rate limiting, robots.txt, Retry-After,
redirect validation, size caps, conditional requests, and the audit log
are each implemented exactly once, and an adapter cannot forget to apply
one because it has no other way to fetch anything.

Adapters receive a fetcher rather than constructing one, which is also
what makes the unit suite offline: tests inject a `FixtureFetcher` that
reads from `tests/fixtures/corpus/` and raises if asked for a URL it does
not have.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

import requests

from .content_signal import ContentSignal, parse_content_signal
from .security import DisallowedURLError, SecurityError, sha256_bytes, sniff_mime, validate_url

logger = logging.getLogger(__name__)


class HttpCache(Protocol):
    """
    The slice of `CorpusRepository` this client needs.

    Declared structurally rather than imported: the repository already
    imports from here, and the fetcher has no business knowing about the
    database beyond these two calls.
    """

    def get_http_cache(self, url: str) -> dict | None: ...

    def set_http_cache(
        self, url: str, etag: str | None, last_modified: str | None,
        status_code: int, content_sha256: str | None,
    ) -> None: ...


class FetchError(RuntimeError):
    """A network fetch failed. `retryable` decides whether resume retries it."""

    def __init__(self, message: str, *, retryable: bool = True, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class NotModified(Exception):
    """The server answered 304: the cached copy is still current."""


class RobotsDisallowed(FetchError):
    """robots.txt forbids this path for our User-Agent."""

    def __init__(self, url: str) -> None:
        super().__init__(f"robots.txt disallows {url}", retryable=False)


@dataclass(frozen=True)
class FetchResponse:
    url: str
    final_url: str
    status_code: int
    content: bytes
    headers: dict[str, str]
    detected_mime: str
    declared_mime: str
    encoding: str
    sha256: str
    from_cache: bool = False

    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


@dataclass
class RateLimiter:
    """
    Per-host request pacing.

    Deliberately simple and synchronous: sleep until the next slot. The
    harvester is not trying to maximise throughput -- it is trying to be a
    guest that institutional servers do not notice. `Retry-After` from a
    429/503 overrides the configured rate for that host until it expires.
    """

    requests_per_minute: float
    _last_request: dict[str, float] = field(default_factory=dict)
    _penalty_until: dict[str, float] = field(default_factory=dict)

    def wait(self, host: str) -> None:
        now = time.monotonic()

        penalty = self._penalty_until.get(host, 0.0)
        if penalty > now:
            sleep_for = penalty - now
            logger.info("Rate limiter: honouring Retry-After for %s (%.1fs)", host, sleep_for)
            time.sleep(sleep_for)
            now = time.monotonic()

        if self.requests_per_minute <= 0:
            return
        min_interval = 60.0 / self.requests_per_minute
        last = self._last_request.get(host)
        if last is not None:
            elapsed = now - last
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
        self._last_request[host] = time.monotonic()

    def penalise(self, host: str, seconds: float) -> None:
        self._penalty_until[host] = time.monotonic() + max(0.0, seconds)


def parse_retry_after(value: str) -> float:
    """
    `Retry-After` is either delta-seconds or an HTTP-date. Both are
    honoured; an unparseable value falls back to a conservative 60s rather
    than to zero, because the server asking us to wait is the signal that
    matters, not the precision of the number.
    """
    value = (value or "").strip()
    if not value:
        return 0.0
    if value.isdigit():
        return float(value)
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(value)
        from datetime import UTC, datetime

        delta = (target - datetime.now(UTC)).total_seconds()
        return max(0.0, delta)
    except Exception:
        logger.warning("Unparseable Retry-After %r; falling back to 60s", value)
        return 60.0


class BaseFetcher:
    """The interface adapters depend on. Kept minimal on purpose."""

    def fetch(
        self,
        url: str,
        *,
        allowed_hosts: list[str],
        max_bytes: int = 0,
        conditional: bool = False,
        allow_http: bool = False,
        accept: str = "",
        byte_range: str = "",
    ) -> FetchResponse:
        """
        `byte_range` is a Range header value ("bytes=100-199").

        It exists for two things that would otherwise be impossible
        within the download budget: resuming an interrupted multi-
        gigabyte dump from where it stopped, and reading one 45 kB
        bzip2 stream out of a 2.8 GB multistream file instead of the
        whole thing. Both are the published, intended way to use those
        files.
        """
        raise NotImplementedError


class BoundedFetcher(BaseFetcher):
    """
    Real network access, bounded on every axis the brief names.

    Notable behaviours:

    * **Redirects are not followed by requests.** They are followed here,
      one hop at a time, re-validating the target against the allowlist
      each time. A redirect off an allowlisted host to an arbitrary one is
      the cheapest way to defeat a domain allowlist, and `allow_redirects=
      True` would hand it over for free.
    * **Size is capped during streaming**, not checked afterwards, so a
      server that lies about Content-Length (or omits it) cannot make the
      harvester buffer a multi-gigabyte body.
    * **robots.txt is fetched once per host and cached** for the process
      lifetime. A host whose robots.txt is unreachable is treated as
      permissive -- that is what the RFC and every major crawler do -- but
      a host that returns a disallow is obeyed absolutely.
    """

    MAX_REDIRECTS = 5

    def __init__(
        self,
        user_agent: str,
        *,
        requests_per_minute: float = 20.0,
        timeout: float = 30.0,
        max_retries: int = 3,
        max_bytes: int = 64 * 1024 * 1024,
        respect_robots: bool = True,
        cache: HttpCache | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self.limiter = RateLimiter(requests_per_minute)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})

        # Keep-alive is counterproductive at harvesting pace. With a
        # polite 6 requests/minute there are ten seconds between calls,
        # by which time the server has usually closed the pooled
        # connection -- and the client only discovers that by having the
        # next request fail with RemoteDisconnected. Observed against
        # Project Gutenberg: every single request failed once and
        # succeeded on retry, doubling our request count against exactly
        # the institution we are trying not to burden.
        #
        # Below roughly one request every two seconds, asking the server
        # to close each connection is both cheaper and more honest than
        # reusing one it has already discarded.
        if 0 < requests_per_minute <= 30:
            self.session.headers["Connection"] = "close"
        #: Optional CorpusRepository, used for ETag/Last-Modified storage.
        self.cache = cache
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        #: Per-origin Content-Signal, parsed from the same robots.txt.
        self._content_signals: dict[str, ContentSignal] = {}
        self._request_log: list[dict] = []
        self.bytes_downloaded = 0
        self.request_count = 0

    # ------------------------------------------------------------- robots

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._robots:
            return self._robots[origin]

        parser = urllib.robotparser.RobotFileParser()
        robots_url = urljoin(origin, "/robots.txt")
        try:
            response = self.session.get(robots_url, timeout=self.timeout, allow_redirects=True)
            if response.status_code >= 400:
                # 404 means "no restrictions stated" per the standard.
                self._robots[origin] = None
                return None
            parser.parse(response.text.splitlines())
            self._robots[origin] = parser
            # Parsed from the same fetch, never a second request. The
            # Disallow rules govern what we may FETCH; a Content-Signal
            # governs what the content may be USED for, and only the
            # second one needs to survive into the document record.
            self._content_signals[origin] = parse_content_signal(
                response.text, urlparse(origin).hostname or ""
            )
            return parser
        except requests.RequestException as exc:
            logger.info("robots.txt unreachable for %s (%s); proceeding under the configured rate limit", origin, exc)
            self._robots[origin] = None
            return None

    def content_signal_for(self, url: str) -> ContentSignal:
        """
        The host's declared conditions of use.

        Fetches robots.txt if it has not been read yet, then answers from
        cache. Returns an undeclared signal when the host published none,
        which is distinct from one that said no -- silence neither grants
        nor restricts.
        """
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._content_signals:
            self._robots_for(url)
        return self._content_signals.get(
            origin, ContentSignal(host=parsed.hostname or "")
        )

    def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    # -------------------------------------------------------------- fetch

    def fetch(
        self,
        url: str,
        *,
        allowed_hosts: list[str],
        max_bytes: int = 0,
        conditional: bool = False,
        allow_http: bool = False,
        accept: str = "",
        byte_range: str = "",
    ) -> FetchResponse:
        limit = max_bytes or self.max_bytes
        current = validate_url(url, allowed_hosts, allow_http=allow_http)

        headers: dict[str, str] = {}
        if accept:
            headers["Accept"] = accept
        if byte_range:
            headers["Range"] = byte_range
        cached = None
        if conditional and self.cache is not None:
            cached = self.cache.get_http_cache(url)
            if cached:
                if cached.get("etag"):
                    headers["If-None-Match"] = cached["etag"]
                if cached.get("last_modified"):
                    headers["If-Modified-Since"] = cached["last_modified"]

        for hop in range(self.MAX_REDIRECTS + 1):
            if not self._robots_allows(current):
                raise RobotsDisallowed(current)

            host = urlparse(current).hostname or ""
            self.limiter.wait(host)
            response = self._request_with_retry(current, headers, limit)

            if response.status_code == 304:
                self._log(current, 304, 0)
                raise NotModified(current)

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                if not location:
                    raise FetchError(f"Redirect from {current} with no Location header", retryable=False)
                target = urljoin(current, location)
                # Re-validate on every hop. This is the whole reason
                # redirects are handled manually.
                try:
                    current = validate_url(target, allowed_hosts, allow_http=allow_http)
                except DisallowedURLError as exc:
                    upgraded = _upgrade_to_https(target)
                    if upgraded is None:
                        raise FetchError(
                            f"Refusing redirect to a non-allowlisted destination: {exc}", retryable=False
                        ) from exc
                    # A server redirecting to a plain-HTTP URL on a host
                    # we already trust is common and is not an attack --
                    # Project Gutenberg's /ebooks/<id>.rdf does exactly
                    # this, then immediately redirects http to https
                    # itself. Refusing outright made those URLs
                    # unfetchable; following the http hop would be a
                    # downgrade. So the scheme is upgraded and
                    # re-validated: we never speak plaintext, and a
                    # server that only offers http still fails.
                    try:
                        current = validate_url(upgraded, allowed_hosts, allow_http=False)
                    except DisallowedURLError:
                        raise FetchError(
                            f"Refusing redirect to a non-allowlisted destination: {exc}", retryable=False
                        ) from exc
                    logger.debug("Upgraded redirect %s -> %s", target, current)
                if hop == self.MAX_REDIRECTS:
                    raise FetchError(f"Too many redirects starting at {url}", retryable=False)
                continue

            if byte_range and response.status_code != 206:
                # The server ignored Range and is sending the whole file.
                # `_read_capped` would hand back the first `limit` bytes
                # of a 2.8 GB dump as though they were the 45 kB stream
                # that was asked for -- a truncated body with a 200 next
                # to it, which nothing downstream could tell from success.
                code = response.status_code
                response.close()
                raise FetchError(
                    f"{url}: asked for {byte_range} and got {code}, not 206; "
                    "the server does not honour Range on this resource",
                    retryable=False,
                    status_code=code,
                )

            content = self._read_capped(response, limit)
            declared = response.headers.get("Content-Type", "")
            sniffed = sniff_mime(content, declared)
            digest = sha256_bytes(content)

            self.bytes_downloaded += len(content)
            self._log(current, response.status_code, len(content))

            if conditional and self.cache is not None:
                self.cache.set_http_cache(
                    url,
                    response.headers.get("ETag"),
                    response.headers.get("Last-Modified"),
                    response.status_code,
                    digest,
                )

            return FetchResponse(
                url=url,
                final_url=current,
                status_code=response.status_code,
                content=content,
                headers=dict(response.headers),
                detected_mime=sniffed.mime,
                declared_mime=declared.split(";")[0].strip(),
                encoding=sniffed.encoding,
                sha256=digest,
            )

        raise FetchError(f"Too many redirects starting at {url}", retryable=False)

    def _request_with_retry(self, url: str, headers: dict[str, str], limit: int):
        last_error: Exception | None = None
        host = urlparse(url).hostname or ""

        for attempt in range(1, self.max_retries + 1):
            try:
                self.request_count += 1
                response = self.session.get(
                    url, headers=headers, timeout=self.timeout, stream=True, allow_redirects=False
                )
            except requests.Timeout as exc:
                last_error = exc
                logger.warning("Timeout fetching %s (attempt %d/%d)", url, attempt, self.max_retries)
            except requests.ConnectionError as exc:
                last_error = exc
                # A connection dropped on the FIRST attempt is almost
                # always a pooled connection the server closed while we
                # were pacing, not a server in trouble. Retrying
                # immediately costs nothing and avoids a pointless
                # backoff; a second failure is treated as a real problem
                # and backs off normally.
                if attempt == 1 and _looks_like_a_stale_connection(exc):
                    logger.debug("Stale pooled connection to %s; retrying immediately", url)
                    continue
                logger.warning("Network error fetching %s (attempt %d/%d): %s", url, attempt, self.max_retries, exc)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Network error fetching %s (attempt %d/%d): %s", url, attempt, self.max_retries, exc)
            else:
                if response.status_code in (429, 503):
                    delay = parse_retry_after(response.headers.get("Retry-After", ""))
                    if delay <= 0:
                        delay = min(60.0, 2.0**attempt)
                    self.limiter.penalise(host, delay)
                    response.close()
                    last_error = FetchError(
                        f"{response.status_code} from {url}; Retry-After {delay:.0f}s",
                        retryable=True,
                        status_code=response.status_code,
                    )
                    if attempt < self.max_retries:
                        self.limiter.wait(host)
                        continue
                    raise last_error
                if response.status_code >= 500:
                    response.close()
                    last_error = FetchError(
                        f"{response.status_code} from {url}", retryable=True, status_code=response.status_code
                    )
                    if attempt < self.max_retries:
                        time.sleep(min(30.0, 2.0**attempt))
                        continue
                    raise last_error
                if response.status_code >= 400:
                    code = response.status_code
                    response.close()
                    # 4xx is the server telling us this will never work.
                    raise FetchError(f"{code} from {url}", retryable=False, status_code=code)
                return response

            if attempt < self.max_retries:
                time.sleep(min(30.0, 2.0**attempt))

        raise FetchError(f"Failed to fetch {url} after {self.max_retries} attempts: {last_error}", retryable=True)

    def _read_capped(self, response, limit: int) -> bytes:
        """
        Stream the body, aborting the moment it exceeds `limit`.

        Content-Length is checked first as a cheap early exit but is never
        trusted as the enforcement mechanism -- the running total is.
        """
        declared_length = response.headers.get("Content-Length")
        if declared_length and declared_length.isdigit() and int(declared_length) > limit:
            response.close()
            raise FetchError(
                f"Refusing {declared_length}-byte body over the {limit}-byte limit", retryable=False
            )

        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    raise FetchError(f"Response body exceeded the {limit}-byte limit", retryable=False)
                chunks.append(chunk)
        finally:
            response.close()
        return b"".join(chunks)

    def _log(self, url: str, status: int, size: int) -> None:
        # URLs only, never headers: an Authorization header or a token in
        # a query string must never reach the log (security rule: "aucun
        # token dans les logs").
        self._request_log.append({"url": _redact(url), "status": status, "bytes": size})

    @property
    def request_log(self) -> list[dict]:
        return list(self._request_log)


_SECRET_PARAM = re.compile(r"([?&](?:key|token|api_key|apikey|access_token|wskey|password)=)[^&]*", re.IGNORECASE)


def _redact(url: str) -> str:
    """Strip credential-bearing query parameters before anything is logged."""
    return _SECRET_PARAM.sub(r"\1REDACTED", url)


#: Signatures of a connection the peer closed while it sat idle in the
#: pool, as opposed to a server that is genuinely refusing or failing.
_STALE_CONNECTION_MARKERS = (
    "remotedisconnected",
    "connection aborted",
    "connection reset by peer",
    "bad handshake",
)


def _looks_like_a_stale_connection(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _STALE_CONNECTION_MARKERS)


def _upgrade_to_https(url: str) -> str | None:
    """
    Rewrite an `http://` URL to `https://`, or None if it is not plain
    HTTP. Used only for redirect targets, and the result is re-validated
    against the allowlist like any other hop -- this widens nothing.
    """
    if url.lower().startswith("http://"):
        return "https://" + url[len("http://") :]
    return None


class FixtureFetcher(BaseFetcher):
    """
    Offline fetcher for the unit suite.

    Maps URLs to local fixture files and raises `FetchError` for any URL
    it was not given -- so a test that accidentally depends on the network
    fails loudly rather than silently reaching out. Allowlist validation
    still runs, which means the adapters' host restrictions are exercised
    by the offline tests too.
    """

    def __init__(self, mapping: dict[str, Path | bytes], *, strict_allowlist: bool = True) -> None:
        self.mapping = mapping
        self.strict_allowlist = strict_allowlist
        self.calls: list[str] = []
        self.bytes_downloaded = 0
        self.request_count = 0
        #: URLs that should raise NotModified, for conditional-request tests.
        self.not_modified: set[str] = set()
        #: URLs that should raise a RETRYABLE failure, simulating a
        #: network interruption. Distinct from simply having no fixture,
        #: which is a 404 and therefore final: an interrupted run must
        #: leave its documents resumable, and a 404 must not.
        self.transient_failures: set[str] = set()
        #: URLs that answer 200-with-the-whole-body to a Range request,
        #: simulating a server that does not honour Range. The real
        #: fetcher must refuse that rather than accept a truncated body.
        self.ignores_range: set[str] = set()
        #: Range headers actually requested, so a test can assert that a
        #: 2.8 GB dump was read as a 45 kB slice and not in full.
        self.ranges: list[tuple[str, str]] = []

    def fetch(
        self,
        url: str,
        *,
        allowed_hosts: list[str],
        max_bytes: int = 0,
        conditional: bool = False,
        allow_http: bool = False,
        accept: str = "",
        byte_range: str = "",
    ) -> FetchResponse:
        if self.strict_allowlist:
            validate_url(url, allowed_hosts, allow_http=allow_http)
        self.calls.append(url)
        self.request_count += 1

        if url in self.not_modified:
            raise NotModified(url)

        if url in self.transient_failures:
            raise FetchError(f"Simulated network interruption for {url}", retryable=True)

        payload = self.mapping.get(url)
        if payload is None:
            raise FetchError(f"FixtureFetcher has no fixture for {url}", retryable=False, status_code=404)

        data = payload.read_bytes() if isinstance(payload, Path) else payload

        status = 200
        if byte_range:
            self.ranges.append((url, byte_range))
            if url in self.ignores_range:
                raise FetchError(
                    f"{url}: asked for {byte_range} and got 200, not 206; "
                    "the server does not honour Range on this resource",
                    retryable=False,
                    status_code=200,
                )
            data = _slice_for_range(data, byte_range)
            status = 206

        if max_bytes and len(data) > max_bytes:
            raise FetchError(f"Fixture body exceeds the {max_bytes}-byte limit", retryable=False)

        try:
            sniffed = sniff_mime(data)
            mime, encoding = sniffed.mime, sniffed.encoding
        except SecurityError:
            raise
        self.bytes_downloaded += len(data)

        return FetchResponse(
            url=url,
            final_url=url,
            status_code=status,
            content=data,
            headers={},
            detected_mime=mime,
            declared_mime="",
            encoding=encoding,
            sha256=sha256_bytes(data),
        )


def _slice_for_range(data: bytes, byte_range: str) -> bytes:
    """
    Apply a `bytes=start-end` header to fixture bytes, inclusive of both
    ends as RFC 9110 specifies. An open-ended `bytes=start-` runs to the
    end, which is the form a resumed download uses.
    """
    spec = byte_range.removeprefix("bytes=").strip()
    start_text, _, end_text = spec.partition("-")
    try:
        start = int(start_text)
    except ValueError:
        raise FetchError(f"Unparseable Range header {byte_range!r}", retryable=False) from None
    if start >= len(data):
        raise FetchError(f"Range {byte_range} is past the end of the body", retryable=False,
                         status_code=416)
    end = int(end_text) if end_text.strip().isdigit() else len(data) - 1
    return data[start:end + 1]


def default_user_agent(contact: str, version: str = "1.0") -> str:
    """
    A User-Agent that identifies the software and a way to reach its
    operator, per every institutional harvesting policy that exists. The
    contact is configuration (FIELDHORIZON_HARVESTER_CONTACT), never a
    hardcoded address.
    """
    contact = (contact or "").strip() or "unconfigured-contact"
    return f"FieldHorizonCorpusHarvester/{version} (+{contact})"


__all__ = [
    "BaseFetcher",
    "BoundedFetcher",
    "FetchError",
    "FetchResponse",
    "FixtureFetcher",
    "NotModified",
    "RateLimiter",
    "RobotsDisallowed",
    "default_user_agent",
    "parse_retry_after",
]
