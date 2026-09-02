"""
Turning downloaded bytes into normalized UTF-8 text.

Format preference, highest first: TXT, EPUB, HTML/XHTML, XML/TEI, PDF
with a usable text layer. OCR is not implemented -- see the module note
at the bottom of this docstring.

Two invariants hold throughout:

* **The raw file is never modified.** Normalization always produces a new
  artifact; the immutable download stays exactly as it arrived, which is
  what makes re-normalization under a later normalizer version possible
  without re-fetching anything.
* **Nothing is rewritten editorially.** Boilerplate and navigation are
  removed, encodings are repaired, Unicode is normalized to NFC. Spelling
  is not modernized, style is not touched, and nothing is summarized. A
  1789 text keeps its 1789 orthography.

Licence text is *extracted and preserved separately* before boilerplate
stripping, never merely deleted -- the licence is evidence, and evidence
that only exists inside a file we then edited is not evidence.

**OCR** is out of scope for this implementation. No OCR engine is
installed, the brief has it disabled by default, and a PDF whose text
layer is unusable is routed to QUALITY_REJECTED with an explicit reason
rather than silently OCR'd at unknown quality. The configuration key
exists and is documented as unimplemented.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import zlib
from dataclasses import dataclass, field

from .ocr import NeedsOCR
from .security import (
    UnsafeArchiveError,
    child_local,
    detect_encoding,
    findall_local,
    html_to_text,
    local_name,
    open_zip,
    safe_parse_xml,
    safe_zip_members,
    safe_zip_read,
    sha256_text,
)

logger = logging.getLogger(__name__)

NORMALIZER_VERSION = "1.0.0"


class NormalizationError(RuntimeError):
    """The payload could not be turned into usable text."""


@dataclass
class NormalizedDocument:
    text: str
    source_format: str
    encoding: str
    #: Licence/rights text found inside the document itself. Preserved
    #: verbatim as evidence, and removed from the body only after it has
    #: been captured here.
    license_text: str = ""
    #: Chapter/section titles in reading order, where the format exposes
    #: them. Feeds the classifier's structural signals.
    structure: list[str] = field(default_factory=list)
    #: Per-format diagnostics -- extraction ratios, page counts, dropped
    #: boilerplate line counts. Surfaced in the quality report.
    metrics: dict = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return sha256_text(self.text)


# --------------------------------------------------------------------------
# Unicode / whitespace normalization
# --------------------------------------------------------------------------

#: Control characters to strip. Tab and newline survive; everything else
#: in the C0/C1 ranges is removed, along with the zero-width and
#: bidi-override characters that can make text render differently from
#: what it says (a genuine, if exotic, spoofing vector in a corpus).
_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤﻿]"
)

_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_MANY_BLANK_LINES_RE = re.compile(r"\n{4,}")
_MANY_SPACES_RE = re.compile(r"[ \t]{3,}")


def normalize_unicode(text: str) -> str:
    """
    UTF-8 / NFC / consistent line endings, controls stripped.

    Curly quotes, em dashes, and ligatures are deliberately preserved:
    they are part of the text as published. NFC (not NFKC) for the same
    reason -- NFKC would rewrite ligatures and superscripts, which is an
    editorial change dressed up as normalization.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = unicodedata.normalize("NFC", text)
    text = _TRAILING_WS_RE.sub("", text)
    text = _MANY_SPACES_RE.sub("  ", text)
    text = _MANY_BLANK_LINES_RE.sub("\n\n\n", text)
    return text.strip()


# Mojibake signatures: UTF-8 bytes decoded as cp1252/latin-1. Repaired
# only when the round-trip is exact, so a text that genuinely contains
# "Ã©" (vanishingly unlikely, but possible in a text about encodings) is
# left alone rather than corrupted by an over-eager fix.
_MOJIBAKE_MARKERS = ("Ã©", "Ã¨", "Ã ", "Ã§", "â€™", "â€œ", "â€\x9d", "Ã¼", "Ã¶", "Ã¤", "Ã±")


def repair_mojibake(text: str) -> tuple[str, bool]:
    """
    Attempt the classic UTF-8-read-as-cp1252 repair. Returns
    (text, repaired). Only applied when markers are present AND the
    re-decode succeeds cleanly.
    """
    if not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text, False
    try:
        repaired = text.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text, False
    if sum(repaired.count(m) for m in _MOJIBAKE_MARKERS) < sum(text.count(m) for m in _MOJIBAKE_MARKERS):
        return repaired, True
    return text, False


# --------------------------------------------------------------------------
# Boilerplate and licence extraction
# --------------------------------------------------------------------------

# Project Gutenberg wraps every text in start/end markers. The licence
# block between them is captured as evidence, then removed from the body:
# leaving it in would pollute every chunk of every Gutenberg text with the
# same 3 kB of legal boilerplate.
_PG_START = re.compile(
    r"^\*{3}\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*{3}\s*$", re.IGNORECASE | re.MULTILINE
)
_PG_END = re.compile(
    r"^\*{3}\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*{3}\s*$", re.IGNORECASE | re.MULTILINE
)


#: Legacy markers. Project Gutenberg's oldest texts (roughly ids under
#: 100, digitised in the 1970s-90s) predate the modern
#: "*** START OF THE PROJECT GUTENBERG EBOOK ***" convention entirely.
#: Without these, every one of those files carries its 1970s preamble
#: into chunk 0 of the corpus -- which is how "the computers we used then
#: didn't have lower case" ended up as retrievable corpus text.
_PG_LEGACY_END_SMALL_PRINT = re.compile(
    r"^\*ENDS?\*?\s*THE SMALL PRINT!.*$", re.IGNORECASE | re.MULTILINE
)
_PG_LEGACY_WELCOME = re.compile(
    r"^\*+\s*Welcome To The World of Free Plain Vanilla Electronic Texts\s*\*+\s*$",
    re.IGNORECASE | re.MULTILINE,
)
#: A bare separator line of asterisks, used by the oldest files to divide
#: their preamble from the work.
_PG_LEGACY_SEPARATOR = re.compile(r"^\s*\*{3,}\s*$", re.MULTILINE)

_PG_LEGACY_FOOTER = re.compile(
    r"^\s*(?:End of (?:the )?Project Gutenberg|End of Project Gutenberg's)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)


#: How far into a body a legacy preamble may be looked for. The notes
#: Gutenberg left inside its oldest texts are a few hundred characters;
#: searching further risks cutting a work whose own opening happens to
#: mention Project Gutenberg.
_LEGACY_PREAMBLE_WINDOW = 1200


def _legacy_preamble_length(body: str) -> int:
    """
    Length of a 1970s-era preamble at the start of `body`, or 0.

    Recognised only when the opening window both names Project Gutenberg
    AND is closed by a bare asterisk separator -- the shape those notes
    actually have. Requiring both keeps an ordinary text that merely
    mentions Gutenberg, or merely contains a line of asterisks, intact.
    """
    window = body[:_LEGACY_PREAMBLE_WINDOW]
    if "project gutenberg" not in window.lower():
        return 0
    separator = _PG_LEGACY_SEPARATOR.search(window)
    return separator.end() if separator else 0


def split_gutenberg_boilerplate(text: str) -> tuple[str, str]:
    """
    Returns (body, license_and_header_text).

    Both halves are kept. The licence half becomes a stored artifact and
    the evidence behind the rights decision; the body is what gets
    chunked and indexed.

    Handles the modern `*** START OF … ***` markers and the legacy
    preambles used by Gutenberg's oldest texts, which have no such
    markers at all.
    """
    start = _PG_START.search(text)
    end = _PG_END.search(text)

    if start or end:
        body_start = start.end() if start else 0
        body_end = end.start() if end else len(text)
        if body_end > body_start:
            # A legacy preamble can sit INSIDE the modern markers. Project
            # Gutenberg re-wrapped its oldest texts in the current
            # start/end markers without removing their 1970s note, so
            # trusting the marker alone still let "the computers we used
            # then didn't have lower case" into the corpus.
            body_start += _legacy_preamble_length(text[body_start:body_end])
            boilerplate = (text[:body_start] + "\n\n" + text[body_end:]).strip()
            return text[body_start:body_end].strip(), boilerplate
        return text, ""

    # No modern markers. Only treat this as a Gutenberg file at all if it
    # says so near the top -- otherwise an ordinary text that happens to
    # contain a line of asterisks would lose its opening.
    head = text[:4000]
    if "project gutenberg" not in head.lower():
        return text, ""

    body_start = 0
    for pattern in (_PG_LEGACY_END_SMALL_PRINT, _PG_LEGACY_WELCOME):
        match = pattern.search(head)
        if match:
            body_start = max(body_start, match.end())

    if body_start == 0:
        separator = _PG_LEGACY_SEPARATOR.search(head)
        if separator:
            body_start = separator.end()

    body_end = len(text)
    footer = _PG_LEGACY_FOOTER.search(text)
    if footer and footer.start() > body_start:
        body_end = footer.start()

    if body_start == 0 and body_end == len(text):
        return text, ""

    boilerplate = (text[:body_start] + "\n\n" + text[body_end:]).strip()
    return text[body_start:body_end].strip(), boilerplate


_LICENSE_BLOCK_RE = re.compile(
    r"(?:^|\n)((?:[^\n]*(?:creative commons|public domain|licen[cs]e|copyright|all rights reserved|"
    r"droits? d'auteur|domaine public)[^\n]*\n?){1,12})",
    re.IGNORECASE,
)


def extract_license_text(text: str, limit: int = 4000) -> str:
    """
    Capture rights-bearing prose found inside the document.

    This is evidence collection, not adjudication: the rights engine
    decides what it means. Bounded so that a document which is *about*
    copyright does not turn its entire body into "licence text".
    """
    found: list[str] = []
    for match in _LICENSE_BLOCK_RE.finditer(text[:200_000]):
        block = match.group(1).strip()
        if block and block not in found:
            found.append(block)
        if sum(len(f) for f in found) > limit:
            break
    return "\n\n".join(found)[:limit]


def strip_repeated_lines(text: str, min_repeats: int = 5) -> tuple[str, int]:
    """
    Remove running headers and footers -- the same short line appearing
    on page after page of a PDF or a chapter-split HTML book.

    Only *short* lines are eligible (a repeated paragraph is a refrain or
    a liturgical response, not boilerplate) and only when they repeat
    often enough to be structural.
    """
    lines = text.split("\n")
    counts: dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if 0 < len(stripped) <= 80:
            counts[stripped] = counts.get(stripped, 0) + 1

    boilerplate = {
        line
        for line, count in counts.items()
        if count >= min_repeats and not _looks_like_content(line)
    }
    if not boilerplate:
        return text, 0

    kept = [line for line in lines if line.strip() not in boilerplate]
    return "\n".join(kept), len(lines) - len(kept)


def _looks_like_content(line: str) -> bool:
    """A repeated line that ends in sentence punctuation is probably real."""
    stripped = line.strip()
    return bool(stripped.endswith((".", "!", "?", "…")) and len(stripped.split()) > 6)


# --------------------------------------------------------------------------
# Per-format extraction
# --------------------------------------------------------------------------


def normalize_txt(data: bytes, declared_encoding: str = "") -> NormalizedDocument:
    encoding = declared_encoding or detect_encoding(data)
    try:
        text = data.decode(encoding, errors="replace")
    except LookupError:
        encoding = "utf-8"
        text = data.decode("utf-8", errors="replace")

    text, repaired = repair_mojibake(text)
    body, boilerplate = split_gutenberg_boilerplate(text)
    license_text = boilerplate or extract_license_text(body)
    body = normalize_unicode(body)
    body, removed = strip_repeated_lines(body)

    return NormalizedDocument(
        text=body,
        source_format="txt",
        encoding=encoding,
        license_text=license_text.strip(),
        structure=_headings_from_plain_text(body),
        metrics={
            "mojibake_repaired": repaired,
            "gutenberg_boilerplate_removed": bool(boilerplate),
            "repeated_lines_removed": removed,
        },
    )


_HEADING_LINE = re.compile(
    r"^(?:chapter|chapitre|kapitel|cap[ií]tulo|capitolo|book|livre|part|partie|section|article)\b.{0,80}$",
    re.IGNORECASE,
)


def _headings_from_plain_text(text: str, limit: int = 200) -> list[str]:
    """
    Best-effort structure recovery from plain text: short lines that look
    like divisions, plus fully-uppercase short lines. Used as a classifier
    signal, never as a chunk boundary.
    """
    headings: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or len(stripped) > 90:
            continue
        if _HEADING_LINE.match(stripped) or (stripped.isupper() and 3 <= len(stripped) <= 90):
            headings.append(stripped)
            if len(headings) >= limit:
                break
    return headings


def normalize_html(data: bytes, declared_encoding: str = "") -> NormalizedDocument:
    encoding = declared_encoding or detect_encoding(data)
    raw = data.decode(encoding, errors="replace")

    # Meta charset can contradict the byte-level sniff; the document's own
    # declaration wins if it parses, since the server header is often a
    # default the author never touched.
    meta_match = re.search(r'<meta[^>]+charset=["\']?([\w-]+)', raw[:4096], re.IGNORECASE)
    if meta_match:
        declared = meta_match.group(1).lower()
        if declared not in (encoding, "utf8") and declared != encoding.replace("-", ""):
            try:
                raw = data.decode(declared, errors="replace")
                encoding = declared
            except LookupError:
                pass

    headings = [
        re.sub(r"\s+", " ", html_to_text(m.group(1))).strip()
        for m in re.finditer(r"<h[1-6][^>]*>(.*?)</h[1-6]>", raw, re.IGNORECASE | re.DOTALL)
    ]
    text = html_to_text(raw)
    text, repaired = repair_mojibake(text)
    license_text = extract_license_text(text)
    text = normalize_unicode(text)
    text, removed = strip_repeated_lines(text)

    return NormalizedDocument(
        text=text,
        source_format="html",
        encoding=encoding,
        license_text=license_text,
        structure=[h for h in headings if h][:200],
        metrics={"mojibake_repaired": repaired, "repeated_lines_removed": removed, "heading_count": len(headings)},
    )


def normalize_epub(data: bytes) -> NormalizedDocument:
    """
    EPUB in reading order: container.xml -> OPF -> spine -> XHTML.

    Reading order matters. Walking the zip's member list instead produces
    a book whose chapters are in alphabetical filename order, which is
    both wrong and very hard to notice from a quality score. Every zip
    read goes through the zip-slip- and bomb-hardened helpers.
    """
    archive = open_zip(data)

    # Validate the WHOLE archive before reading any part of it. Checking
    # only the members the spine names would let a traversal or bomb
    # member ride along unnoticed simply because nothing referenced it --
    # and the archive is written to disk as a raw artifact regardless of
    # which members get read.
    safe_zip_members(archive)

    try:
        container = safe_parse_xml(safe_zip_read(archive, "META-INF/container.xml"))
    except (KeyError, UnsafeArchiveError) as exc:
        raise NormalizationError(f"EPUB has no readable META-INF/container.xml: {exc}") from exc

    rootfiles = findall_local(container, "rootfile")
    opf_path = ""
    for rootfile in rootfiles:
        opf_path = rootfile.attrib.get("full-path", "")
        if opf_path:
            break
    if not opf_path:
        raise NormalizationError("EPUB container.xml declares no rootfile")

    try:
        opf = safe_parse_xml(safe_zip_read(archive, opf_path))
    except (KeyError, UnsafeArchiveError) as exc:
        raise NormalizationError(f"EPUB OPF unreadable at {opf_path!r}: {exc}") from exc

    base = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""

    manifest: dict[str, str] = {}
    manifest_el = child_local(opf, "manifest")
    for item in findall_local(manifest_el, "item") if manifest_el is not None else []:
        item_id = item.attrib.get("id", "")
        href = item.attrib.get("href", "")
        if item_id and href:
            manifest[item_id] = f"{base}/{href}" if base else href

    spine_el = child_local(opf, "spine")
    spine_ids = [
        el.attrib.get("idref", "")
        for el in (findall_local(spine_el, "itemref") if spine_el is not None else [])
    ]

    sections: list[str] = []
    headings: list[str] = []
    apparatus: list[str] = []
    read_failures = 0
    for idref in spine_ids:
        spine_href = manifest.get(idref)
        if not spine_href:
            continue
        normalized_href = _normalize_zip_path(spine_href)
        try:
            member = safe_zip_read(archive, normalized_href)
        except (KeyError, UnsafeArchiveError) as exc:
            logger.info("EPUB spine member %r unreadable: %s", normalized_href, exc)
            read_failures += 1
            continue
        chunk = normalize_html(member)
        if not chunk.text.strip():
            continue
        if _is_edition_apparatus(idref, normalized_href):
            # Colophons and uncopyright pages describe the EDITION, not
            # the work. They are captured as licence evidence and kept
            # out of the body -- otherwise every Standard Ebooks text
            # ends with several hundred words about copyright law, which
            # then becomes retrievable corpus content.
            apparatus.append(chunk.text)
            continue
        sections.append(chunk.text)
        headings.extend(chunk.structure)

    if not sections:
        raise NormalizationError("EPUB spine produced no readable text")

    text = normalize_unicode("\n\n".join(sections))
    # The edition's own apparatus is the best licence evidence the file
    # contains, so it is preferred over scavenging the body for
    # rights-shaped prose.
    license_text = "\n\n".join(apparatus).strip() or extract_license_text(text)
    text, removed = strip_repeated_lines(text)

    return NormalizedDocument(
        text=text,
        source_format="epub",
        encoding="utf-8",
        license_text=license_text[:8000],
        structure=headings[:400],
        metrics={
            "spine_items": len(spine_ids),
            "spine_items_read": len(sections),
            "spine_apparatus_excluded": len(apparatus),
            "spine_read_failures": read_failures,
            "repeated_lines_removed": removed,
        },
    )


#: EPUB spine items that describe the edition rather than carrying the
#: work: colophons, uncopyright/licence pages, and imprints. Matched on
#: the manifest id and the filename, both of which publishers name
#: conventionally.
_EDITION_APPARATUS = ("colophon", "uncopyright", "imprint", "copyright-page", "copyright_page")


def _is_edition_apparatus(idref: str, href: str) -> bool:
    haystack = f"{idref} {href}".lower()
    return any(marker in haystack for marker in _EDITION_APPARATUS)


def _normalize_zip_path(path: str) -> str:
    """Collapse `a/../b` inside a zip href without touching the filesystem."""
    parts: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


#: TEI elements whose content is apparatus, not the work.
_TEI_DROP = frozenset({"teiheader", "note", "fw", "figdesc", "app", "back"})


def normalize_xml(data: bytes) -> NormalizedDocument:
    """
    XML/TEI to text.

    TEI is handled specially -- `teiHeader` is metadata and `fw` is a
    forme work (running head), so including them would inject the same
    apparatus into the body that the PDF path works to strip out. Any
    other XML falls back to a depth-first text walk.
    """
    root = safe_parse_xml(data)
    is_tei = local_name(root.tag).lower() == "tei" or find_tei_text(root) is not None

    headings: list[str] = []
    parts: list[str] = []

    def walk(element, depth: int = 0) -> None:
        name = local_name(element.tag).lower()
        if name in _TEI_DROP:
            return
        if name in ("head", "title") and element.text:
            heading = " ".join(element.text.split())
            if heading:
                headings.append(heading)
        if element.text and element.text.strip():
            parts.append(element.text)
        for child in element:
            walk(child, depth + 1)
            if child.tail and child.tail.strip():
                parts.append(child.tail)
        if name in ("p", "div", "lg", "l", "head", "sp", "item"):
            parts.append("\n")

    target = find_tei_text(root) if is_tei else root
    walk(target if target is not None else root)

    text = normalize_unicode(re.sub(r"[ \t]+", " ", "".join(parts)))
    license_text = extract_license_text(text)
    text, removed = strip_repeated_lines(text)

    if not text.strip():
        raise NormalizationError("XML document produced no text content")

    return NormalizedDocument(
        text=text,
        source_format="tei" if is_tei else "xml",
        encoding="utf-8",
        license_text=license_text,
        structure=headings[:400],
        metrics={"tei": is_tei, "repeated_lines_removed": removed},
    )


def find_tei_text(root):
    for element in root.iter():
        if local_name(element.tag).lower() == "text":
            return element
    return None


# --------------------------------------------------------------------------
# PDF text layer
# --------------------------------------------------------------------------

_PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_PDF_TEXT_SHOW = re.compile(rb"\((?:\\.|[^\\()])*\)\s*Tj|\[(?:[^\[\]\\]|\\.)*\]\s*TJ", re.DOTALL)
_PDF_STRING = re.compile(rb"\((?:\\.|[^\\()])*\)", re.DOTALL)


def normalize_pdf(data: bytes) -> NormalizedDocument:
    """
    Extract an existing PDF text layer. Deliberately minimal.

    This handles the common case -- Flate-compressed content streams with
    `Tj`/`TJ` text-showing operators and standard encodings -- and nothing
    more. It does not implement CID fonts, custom `ToUnicode` CMaps,
    column detection, or ligature reconstruction. That is a considered
    trade: PDF is last in the format preference order, a non-PDF
    alternative is preferred whenever one exists, and adding a full PDF
    stack as a dependency for the residual case is out of proportion.

    What matters is that this function is *honest about failing*. It
    reports `extraction_quality`, and quality.py rejects a document whose
    text layer did not really come out -- rather than passing a page of
    mangled glyph soup into the corpus. A scanned PDF with no text layer
    at all raises, and the document is rejected with a clear reason
    instead of being silently OCR'd at unknown quality.
    """
    if not data.startswith(b"%PDF-"):
        raise NormalizationError("Not a PDF")

    fragments: list[str] = []
    streams_total = 0
    streams_decoded = 0

    for match in _PDF_STREAM_RE.finditer(data):
        streams_total += 1
        raw = match.group(1)
        decoded: bytes | None = None
        try:
            decoded = zlib.decompress(raw)
            streams_decoded += 1
        except zlib.error:
            # Uncompressed content streams are legal and common in
            # hand-built or linearized PDFs.
            if b"Tj" in raw or b"TJ" in raw:
                decoded = raw
                streams_decoded += 1
        if decoded is None:
            continue
        fragments.extend(_pdf_text_from_content_stream(decoded))

    text = normalize_unicode("\n".join(fragments))
    text, removed = strip_repeated_lines(text, min_repeats=3)

    if not text.strip():
        # NOT a NormalizationError. A malformed EPUB will still be
        # malformed next week; a scan is a perfectly good document whose
        # text simply has not been extracted, and calling that a failure
        # is what made Phase I report page images as broken files.
        raise NeedsOCR(
            "the PDF carries page images and no text layer",
            pages=streams_total,
        )

    # A crude but effective signal: real prose is mostly letters and
    # spaces. Glyph soup from an unsupported encoding is not.
    letters = sum(1 for ch in text if ch.isalpha() or ch.isspace())
    quality = letters / max(1, len(text))

    return NormalizedDocument(
        text=text,
        source_format="pdf",
        encoding="utf-8",
        license_text=extract_license_text(text),
        structure=_headings_from_plain_text(text),
        metrics={
            "pdf_streams": streams_total,
            "pdf_streams_decoded": streams_decoded,
            "extraction_quality": round(quality, 4),
            "repeated_lines_removed": removed,
        },
    )


_PDF_ESCAPES = {
    b"\\n": b"\n", b"\\r": b"\r", b"\\t": b"\t", b"\\b": b"\b",
    b"\\f": b"\f", b"\\(": b"(", b"\\)": b")", b"\\\\": b"\\",
}


def _pdf_text_from_content_stream(stream: bytes) -> list[str]:
    """
    Pull the literal strings out of `Tj` / `TJ` operators.

    `TJ` arrays interleave strings with kerning numbers; the numbers are
    dropped. A large negative kern usually means a word space, which is
    why a space is inserted between array elements -- without it, PDF
    text extraction runs words together.
    """
    out: list[str] = []
    for op_match in _PDF_TEXT_SHOW.finditer(stream):
        segment = op_match.group(0)
        pieces = [_decode_pdf_string(s.group(0)) for s in _PDF_STRING.finditer(segment)]
        if not pieces:
            continue
        joined = ("" if segment.rstrip().endswith(b"Tj") else " ").join(pieces)
        if joined.strip():
            out.append(joined)
    return out


def _decode_pdf_string(raw: bytes) -> str:
    body = raw[1:-1]
    for escape, replacement in _PDF_ESCAPES.items():
        body = body.replace(escape, replacement)
    body = re.sub(rb"\\([0-7]{1,3})", lambda m: bytes([int(m.group(1), 8) & 0xFF]), body)
    if body.startswith(b"\xfe\xff"):
        return body.decode("utf-16-be", errors="replace")
    # PDFDocEncoding is Latin-1-compatible over the printable range, which
    # is what the overwhelming majority of Western-language PDFs use.
    return body.decode("latin-1", errors="replace")


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

#: Preference order used when a source offers several formats.
FORMAT_PREFERENCE = ("txt", "epub", "html", "xhtml", "xml", "tei", "pdf")

_MIME_TO_NORMALIZER = {
    "text/plain": normalize_txt,
    "text/markdown": normalize_txt,
    "application/epub+zip": lambda data, encoding="": normalize_epub(data),
    "text/html": normalize_html,
    "application/xhtml+xml": normalize_html,
    "application/xml": lambda data, encoding="": normalize_xml(data),
    "text/xml": lambda data, encoding="": normalize_xml(data),
    "application/pdf": lambda data, encoding="": normalize_pdf(data),
}


def normalize(data: bytes, mime: str, encoding: str = "") -> NormalizedDocument:
    """
    Normalize by *sniffed* MIME, never by the URL's extension or the
    server's Content-Type -- both lie routinely, and acting on either is
    how a ZIP ends up parsed as text.
    """
    normalizer = _MIME_TO_NORMALIZER.get(mime)
    if normalizer is None:
        raise NormalizationError(f"No normalizer for MIME type {mime!r}")
    try:
        return normalizer(data, encoding)
    except TypeError:
        return normalizer(data)


def rank_format(fmt: str) -> int:
    """Lower is better. Unknown formats sort last."""
    fmt = (fmt or "").lower().lstrip(".")
    aliases = {"text": "txt", "txt.utf-8": "txt", "plain": "txt", "htm": "html"}
    fmt = aliases.get(fmt, fmt)
    try:
        return FORMAT_PREFERENCE.index(fmt)
    except ValueError:
        return len(FORMAT_PREFERENCE)


__all__ = [
    "FORMAT_PREFERENCE",
    "NORMALIZER_VERSION",
    "NormalizationError",
    "NormalizedDocument",
    "extract_license_text",
    "normalize",
    "normalize_epub",
    "normalize_html",
    "normalize_pdf",
    "normalize_txt",
    "normalize_unicode",
    "normalize_xml",
    "rank_format",
    "repair_mojibake",
    "split_gutenberg_boilerplate",
    "strip_repeated_lines",
]
