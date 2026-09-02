"""
Security primitives for handling untrusted third-party content.

The governing assumption of this whole module is that everything the
harvester downloads is hostile until proven otherwise. Not "probably
fine": hostile. That produces a few rules worth stating plainly, because
they are the ones that get quietly broken by well-meaning refactors:

* No downloaded byte is ever executed, rendered, or interpreted as
  markup that could execute. HTML is parsed to text by a stdlib tokenizer
  that has no scripting concept at all.
* No URL found *inside* a document is ever fetched. Only URLs an adapter
  extracted from a catalogue record, against a per-source domain
  allowlist, are ever requested (rule 14).
* No metadata value ever reaches a shell, a SQL string, or a filesystem
  path unsanitized.
* XML is parsed with entity declarations refused outright, which kills
  XXE and billion-laughs at the door rather than mitigating them.

`defusedxml` would cover the XML case; it is deliberately not used, so
that the harvester adds no new dependency to a project pinned at seven.
The wrapper below is narrower than defusedxml but is exact about what it
refuses, and its refusals are tested.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

logger = logging.getLogger(__name__)


class SecurityError(RuntimeError):
    """A downloaded artifact or URL violated a security invariant."""


class UnsafeArchiveError(SecurityError):
    """An archive attempted path traversal, or would expand past its limit."""


class UnsafeXMLError(SecurityError):
    """XML containing a DOCTYPE or entity declaration was refused."""


class DisallowedURLError(SecurityError):
    """A URL failed scheme, host, or allowlist validation."""


# --------------------------------------------------------------------------
# URL validation
# --------------------------------------------------------------------------


def _host_matches(host: str, pattern: str) -> bool:
    """
    Exact host, or a subdomain of a leading-dot pattern.

    `.gutenberg.org` matches `www.gutenberg.org` and `gutenberg.org`;
    it does not match `notgutenberg.org` or `gutenberg.org.evil.com`.
    Suffix matching without the dot boundary is the classic way an
    allowlist gets bypassed, so it is not offered at all.
    """
    host = host.lower().strip(".")
    pattern = pattern.lower().strip()
    if not pattern:
        return False
    if pattern.startswith("."):
        bare = pattern[1:]
        return host == bare or host.endswith("." + bare)
    return host == pattern


def host_matches(host: str, pattern: str) -> bool:
    """
    Public form of the allowlist predicate, for callers that need to test
    a host without building a URL -- Europeana checks whether a provider's
    content host is allowlisted before deciding whether to record it as a
    download target at all.
    """
    return _host_matches(host, pattern)


def host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    return any(_host_matches(host, pattern) for pattern in (allowed_hosts or []))


def validate_url(url: str, allowed_hosts: list[str], *, allow_http: bool = False) -> str:
    """
    Check scheme and host against a per-source allowlist.

    HTTPS only unless a source explicitly opts into plain HTTP in its
    configuration (some institutional OAI-PMH endpoints still have no TLS;
    that must be a deliberate, per-source, documented choice rather than a
    global fallback).
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme == "http" and not allow_http:
        raise DisallowedURLError(f"Refusing plain HTTP (allow_http is not set for this source): {url}")
    if scheme not in ("https", "http"):
        raise DisallowedURLError(f"Refusing non-HTTP(S) scheme {scheme!r}: {url}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise DisallowedURLError(f"URL has no host: {url}")
    if not allowed_hosts:
        raise DisallowedURLError(f"No host allowlist configured; refusing {url}")
    if not any(_host_matches(host, pattern) for pattern in allowed_hosts):
        raise DisallowedURLError(f"Host {host!r} is not in this source's allowlist: {allowed_hosts}")
    return url


# --------------------------------------------------------------------------
# MIME sniffing
# --------------------------------------------------------------------------

#: Magic-number prefixes for formats we refuse outright. These are
#: executables and macro-bearing container formats -- nothing in a text
#: corpus has any business being one (security rules: "refus des
#: exécutables", "refus des formats contenant macros").
_FORBIDDEN_MAGIC: list[tuple[bytes, str]] = [
    (b"MZ", "application/x-dosexec"),
    (b"\x7fELF", "application/x-elf"),
    (b"\xca\xfe\xba\xbe", "application/java-vm"),
    (b"\xfe\xed\xfa\xce", "application/x-mach-binary"),
    (b"\xfe\xed\xfa\xcf", "application/x-mach-binary"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),  # legacy Office w/ macros
    (b"#!", "text/x-script"),
]

_TEXT_MIME = "text/plain"


@dataclass(frozen=True)
class SniffResult:
    mime: str
    encoding: str
    #: Set when the sniffed type contradicts what the server declared.
    #: Not fatal on its own -- servers mislabel constantly -- but the
    #: sniffed value is what the pipeline acts on, and the mismatch is
    #: recorded.
    declared_mismatch: bool = False


def sniff_mime(data: bytes, declared: str = "") -> SniffResult:
    """
    Determine the real type of a payload from its bytes.

    The server's Content-Type is advisory only. A `.txt` URL that returns
    a ZIP, or a `text/html` header over a PDF, is routed by what the bytes
    actually are -- which is the whole point of the exercise.
    """
    head = data[:512]

    for magic, name in _FORBIDDEN_MAGIC:
        if head.startswith(magic):
            raise SecurityError(f"Refusing executable/macro-bearing payload (sniffed {name})")

    declared_base = (declared or "").split(";")[0].strip().lower()

    if head.startswith(b"%PDF-"):
        return SniffResult("application/pdf", "", declared_base not in ("", "application/pdf"))

    if head.startswith(b"PK\x03\x04"):
        # Could be EPUB, ODT, DOCX, or a plain zip. EPUB declares itself
        # in an uncompressed `mimetype` member at a fixed offset.
        if b"application/epub+zip" in data[:200]:
            return SniffResult("application/epub+zip", "", declared_base not in ("", "application/epub+zip"))
        if b"word/" in data[:4096] or b"vbaProject" in data[:8192]:
            raise SecurityError("Refusing OOXML payload (may contain macros)")
        return SniffResult("application/zip", "", declared_base not in ("", "application/zip"))

    if head.startswith(b"\x1f\x8b"):
        return SniffResult("application/gzip", "", declared_base not in ("", "application/gzip"))

    if head.startswith(b"BZh"):
        return SniffResult("application/x-bzip2", "", False)

    encoding = detect_encoding(head)
    text_head = head.decode(encoding, errors="replace").lstrip().lower()

    if text_head.startswith("<?xml") or re.match(r"^<(rdf|feed|oai-pmh|tei|records?|searchretrieve)", text_head):
        return SniffResult("application/xml", encoding, declared_base not in ("", "application/xml", "text/xml"))
    if text_head.startswith("<!doctype html") or text_head.startswith("<html") or "<body" in text_head:
        return SniffResult("text/html", encoding, declared_base not in ("", "text/html"))
    if text_head.startswith("{") or text_head.startswith("["):
        return SniffResult("application/json", encoding, False)

    # Binary heuristic: NUL bytes essentially never occur in real text.
    if b"\x00" in head:
        raise SecurityError("Refusing payload: binary content with no recognized text format")

    return SniffResult(_TEXT_MIME, encoding, declared_base not in ("", "text/plain", "text/markdown"))


_BOMS: list[tuple[bytes, str]] = [
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
]


def detect_encoding(data: bytes) -> str:
    """
    BOM first, then a UTF-8 validity check, then Latin-1 as the terminal
    fallback (it cannot fail, which is why it is last rather than
    preferred). No chardet-style statistical guessing: for a corpus that
    prefers UTF-8 sources and records what it did, a deterministic ladder
    is more auditable than a probabilistic one.
    """
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return encoding
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError as exc:
        # A sample taken from the middle of a stream can split a
        # multi-byte sequence at its END, which is a truncation artifact
        # rather than evidence that the payload is not UTF-8. Retry
        # without the tail ONLY when the failure is genuinely in those
        # last few bytes.
        #
        # Checking exc.start is the whole point: retrying unconditionally
        # made `b"h\xe9llo"` (Latin-1) decode its one-byte prefix `b"h"`
        # successfully and report UTF-8, silently mis-decoding every
        # short Latin-1 payload.
        # The 64-byte floor matters as much as the position check: on a
        # 5-byte payload, trimming to a 1-byte prefix "succeeds" and
        # proves nothing. A boundary-split artifact only arises on a real
        # sample, which is never that small.
        if len(data) >= 64 and exc.start >= len(data) - 4:
            try:
                data[: exc.start].decode("utf-8")
                return "utf-8"
            except UnicodeDecodeError:
                pass
    return "latin-1"


# --------------------------------------------------------------------------
# Safe XML
# --------------------------------------------------------------------------

_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
_ENTITY_RE = re.compile(rb"<!ENTITY", re.IGNORECASE)

#: Refuse XML larger than this before parsing (bytes). Catalogue records
#: and OPDS pages are kilobytes; anything at this scale is either a dump
#: (handled by the streaming dump path, not here) or an attack.
MAX_XML_BYTES = 64 * 1024 * 1024


def safe_parse_xml(data: bytes | str, *, max_bytes: int = MAX_XML_BYTES) -> ElementTree.Element:
    """
    Parse XML with external entities and DTDs refused outright.

    Rather than configuring the parser to *resist* entity expansion, the
    declarations are rejected before parsing begins. That is a stricter
    contract and a far easier one to verify: there is no expansion budget
    to tune and no parser-version-dependent behaviour to reason about.
    Legitimate OPDS, OAI-PMH, RDF, SRU, and TEI records do not carry
    DOCTYPEs.
    """
    raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else data

    if len(raw) > max_bytes:
        raise UnsafeXMLError(f"XML payload of {len(raw)} bytes exceeds the {max_bytes}-byte limit")

    head = raw[:8192]
    if _DOCTYPE_RE.search(head):
        raise UnsafeXMLError("Refusing XML containing a DOCTYPE declaration")
    if _ENTITY_RE.search(raw):
        raise UnsafeXMLError("Refusing XML containing an ENTITY declaration")

    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise UnsafeXMLError(f"Malformed XML: {exc}") from exc


def local_name(tag: str) -> str:
    """`{http://www.w3.org/2005/Atom}entry` -> `entry`."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def findall_local(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    """
    Namespace-agnostic descendant search.

    Real-world OPDS and OAI-PMH feeds disagree about namespace URIs far
    more than they disagree about element names, and hardcoding a
    namespace map is how an adapter silently returns zero results against
    a perfectly valid feed.
    """
    return [el for el in element.iter() if local_name(el.tag) == name]


def find_local(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    for el in element.iter():
        if local_name(el.tag) == name:
            return el
    return None


def child_local(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    """Direct children only -- used where nesting depth is meaningful."""
    for el in element:
        if local_name(el.tag) == name:
            return el
    return None


def children_local(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [el for el in element if local_name(el.tag) == name]


# --------------------------------------------------------------------------
# Safe archives
# --------------------------------------------------------------------------

#: Refuse to expand an archive member past this, and refuse the archive
#: entirely if its declared total exceeds it. Guards decompression bombs.
DEFAULT_MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024

#: A compression ratio above this is a bomb signature. Real EPUBs of
#: prose land around 3-5x; 200x is not a book.
MAX_COMPRESSION_RATIO = 200


def _is_safe_member_name(name: str) -> bool:
    """
    Reject absolute paths, traversal, drive letters, and backslash
    separators. Zip-slip in one predicate.
    """
    if not name or name.endswith("/"):
        return False
    if name.startswith("/") or name.startswith("\\"):
        return False
    if ":" in name.split("/")[0]:
        return False
    normalized = name.replace("\\", "/")
    parts = normalized.split("/")
    return not any(part in ("..", ".") for part in parts)


def safe_zip_members(archive: zipfile.ZipFile, max_total_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES) -> list[str]:
    """
    The names in `archive` that are safe to read, with bomb checks applied.

    Raises rather than silently skipping when the archive as a whole looks
    like a bomb: a zip whose declared expansion is 4 GB is not a
    partially-usable EPUB, it is a refusal.
    """
    total_declared = 0
    safe: list[str] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        if not _is_safe_member_name(info.filename):
            raise UnsafeArchiveError(f"Archive member escapes its root: {info.filename!r}")
        total_declared += info.file_size
        if total_declared > max_total_bytes:
            raise UnsafeArchiveError(
                f"Archive declares {total_declared} bytes decompressed, over the {max_total_bytes}-byte limit"
            )
        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > MAX_COMPRESSION_RATIO and info.file_size > 1024 * 1024:
                raise UnsafeArchiveError(
                    f"Archive member {info.filename!r} has a {ratio:.0f}x compression ratio (decompression bomb)"
                )
        safe.append(info.filename)
    return safe


def safe_zip_read(archive: zipfile.ZipFile, name: str, max_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES) -> bytes:
    """
    Read one member with a hard cap enforced *during* streaming, not from
    the header -- a zip header can lie about `file_size`, and trusting it
    is how the ratio check above gets bypassed.
    """
    if not _is_safe_member_name(name):
        raise UnsafeArchiveError(f"Refusing unsafe archive member name {name!r}")
    with archive.open(name, "r") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UnsafeArchiveError(f"Archive member {name!r} expands past the {max_bytes}-byte limit")
    return data


def open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnsafeArchiveError(f"Not a readable archive: {exc}") from exc


# --------------------------------------------------------------------------
# Safe HTML -> text
# --------------------------------------------------------------------------

#: Subtrees dropped entirely -- content as well as tags. `script` and
#: `style` are the security-relevant ones; the rest are boilerplate that
#: would otherwise pollute the corpus text.
_DROP_SUBTREES = frozenset({"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe"})

_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "hr", "li", "tr", "td", "th", "section", "article",
        "blockquote", "pre", "figure", "figcaption", "dd", "dt",
    }
)

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


class _TextExtractor(HTMLParser):
    """
    HTML to plain text using only the stdlib tokenizer.

    This never builds a DOM, never resolves a URL, never executes
    anything, and has no concept of scripting -- `<script>` content is
    discarded as text, not evaluated, because there is nothing here that
    could evaluate it. Heading structure is preserved as blank-line-
    separated blocks so the normalizer downstream can still see a
    document's shape.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._drop_depth = 0
        self._drop_tag = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._drop_depth:
            if tag == self._drop_tag:
                self._drop_depth += 1
            return
        if tag in _DROP_SUBTREES:
            self._drop_depth = 1
            self._drop_tag = tag
            return
        if tag in _BLOCK_TAGS or tag in _HEADING_TAGS:
            self.parts.append("\n\n" if tag in _HEADING_TAGS else "\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._drop_depth:
            if tag == self._drop_tag:
                self._drop_depth -= 1
                if self._drop_depth == 0:
                    self._drop_tag = ""
            return
        if tag in _BLOCK_TAGS or tag in _HEADING_TAGS:
            self.parts.append("\n\n" if tag in _HEADING_TAGS else "\n")

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        self.parts.append(data)

    # Comments can carry conditional-comment markup; they are never text.
    def handle_comment(self, data: str) -> None:
        return

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = re.sub(r"[ \t\f\v]+", " ", joined)
        joined = re.sub(r" *\n *", "\n", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # HTMLParser can raise on pathological input
        logger.warning("HTML extraction stopped early: %s", exc)
    return parser.text()


# --------------------------------------------------------------------------
# Filesystem safety
# --------------------------------------------------------------------------


def safe_filename(name: str, *, max_length: int = 120, default: str = "document") -> str:
    """
    Reduce an untrusted string to a leaf filename that cannot escape its
    directory or surprise a shell.

    Everything outside a conservative character class is replaced, path
    separators included, and the result is truncated. Windows reserved
    device names are rejected even on Linux -- corpora get copied to other
    machines, and a file named `CON.txt` becomes someone else's problem.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    # Collapse consecutive dots. Without a path separator ".." cannot
    # traverse anything, but a filename containing it is confusing to
    # every reader and every tool that later globs this directory, and no
    # legitimate title needs it.
    cleaned = re.sub(r"\.{2,}", "_", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    if len(cleaned) > max_length:
        stem, dot, suffix = cleaned.rpartition(".")
        if dot and len(suffix) <= 8:
            cleaned = stem[: max_length - len(suffix) - 1] + "." + suffix
        else:
            cleaned = cleaned[:max_length]
    reserved = {
        "con", "prn", "aux", "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
    if cleaned.split(".")[0].lower() in reserved:
        cleaned = f"_{cleaned}"
    return cleaned or default


def ensure_within(base: Path, target: Path) -> Path:
    """
    Assert that `target` resolves inside `base`.

    Used at every filesystem write. Any path built from harvested metadata
    passes through here before it is opened, so a name that survived
    `safe_filename` and still somehow escapes is caught at the boundary.
    """
    base_resolved = base.resolve()
    target_resolved = target.resolve()
    if base_resolved != target_resolved and base_resolved not in target_resolved.parents:
        raise SecurityError(f"Refusing to write outside {base_resolved}: {target_resolved}")
    return target_resolved


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_MAX_DECOMPRESSED_BYTES",
    "DisallowedURLError",
    "SecurityError",
    "SniffResult",
    "UnsafeArchiveError",
    "UnsafeXMLError",
    "child_local",
    "children_local",
    "detect_encoding",
    "ensure_within",
    "find_local",
    "findall_local",
    "host_allowed",
    "host_matches",
    "html_to_text",
    "local_name",
    "open_zip",
    "safe_filename",
    "safe_parse_xml",
    "safe_zip_members",
    "safe_zip_read",
    "sha256_bytes",
    "sha256_text",
    "sniff_mime",
    "validate_url",
]
