"""
Shared helpers for the corpus-harvester tests.

Text fixtures are committed files under `tests/fixtures/corpus/`; binary
ones (EPUB, PDF, zip archives) are *built here, deterministically*, at
test time. That is a considered choice: a committed 200 kB binary blob is
unreviewable in a diff, whereas the builders below are readable, and a
reviewer can see exactly what an "EPUB with a zip-slip member" contains
without unzipping anything. Every builder is pure -- same input, same
bytes -- so tests stay reproducible.

Nothing here touches the network. `make_fetcher` returns a
`FixtureFetcher` that raises for any URL it was not explicitly given,
which is what makes an accidental network dependency fail loudly rather
than silently pass on a machine that happens to be online.
"""

from __future__ import annotations

import bz2
import io
import re
import tempfile
import zipfile
from pathlib import Path

from fieldhorizon.config import AppConfig
from fieldhorizon.corpus.adapters.base import SourceConfig
from fieldhorizon.corpus.http import FixtureFetcher
from fieldhorizon.corpus.policy import Budget, ExportPolicy, HarvesterPolicy
from fieldhorizon.corpus.quality import QualityThresholds
from fieldhorizon.corpus.selection import SelectionWeights

FIXTURES = Path(__file__).parent / "fixtures" / "corpus"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Config / policy
# --------------------------------------------------------------------------


def make_config(tmp_path: Path) -> AppConfig:
    """
    An AppConfig rooted at a temporary directory.

    Mirrors the shape the existing test modules build (test_manifests.py
    and friends), so the harvester tests exercise the same construction
    path the rest of the suite does.
    """
    return AppConfig(
        root=tmp_path,
        database=tmp_path / "data" / "field_horizon.sqlite3",
        books=tmp_path / "data" / "books",
        json_corpus=tmp_path / "data" / "json_corpus",
        outputs=tmp_path / "outputs",
        logs=tmp_path / "logs",
        ollama_base_url="http://localhost:11434",
        default_model="hermes3:8b",
        temperature=1.25,
        top_p=0.95,
        repeat_penalty=1.08,
        num_ctx=8192,
        book_fragments=6,
        json_entries=8,
        chunk_chars=1800,
        chunk_overlap=250,
        tone="dark",
        mode="canonical_synthesis",
        manifestos=tmp_path / "data" / "manifestos",
        embedding_model="nomic-embed-text",
    )


def make_policy(**overrides) -> HarvesterPolicy:
    """
    A policy with a configured budget, so tests exercise the normal path
    rather than the not-configured refusal. Tests that want the refusal
    zero the relevant field explicitly.
    """
    budget = Budget(
        max_items_per_run=overrides.pop("max_items_per_run", 10),
        max_download_bytes_per_run=overrides.pop("max_download_bytes_per_run", 50 * 1024 * 1024),
        max_total_corpus_bytes=overrides.pop("max_total_corpus_bytes", 1024 * 1024 * 1024),
        min_free_disk_bytes=overrides.pop("min_free_disk_bytes", 1024),
        requests_per_minute=overrides.pop("requests_per_minute", 6000.0),
        max_candidates_per_source=overrides.pop("max_candidates_per_source", 100),
    )
    policy = HarvesterPolicy(
        profile=overrides.pop("profile", "broad"),
        rights_profile=overrides.pop("rights_profile", "local_research_us"),
        budget=budget,
        selection_weights=SelectionWeights(),
        quality=overrides.pop("quality", QualityThresholds()),
        # Off by default across the suite: the unit tests must never
        # depend on a reachable Ollama, and the deterministic classifier
        # is the documented fallback anyway.
        use_llm_classifier=overrides.pop("use_llm_classifier", False),
        export=ExportPolicy(),
        respect_robots=False,
    )
    for key, value in overrides.items():
        setattr(policy, key, value)
    return policy


def make_source(
    source_id: str = "test_source",
    adapter: str = "opds",
    *,
    base_url: str = "https://standardebooks.org/feeds/opds/all",
    allowed_hosts: list[str] | None = None,
    **overrides,
) -> SourceConfig:
    return SourceConfig(
        source_id=source_id,
        adapter=adapter,
        display_name=overrides.pop("display_name", source_id),
        enabled=overrides.pop("enabled", True),
        base_url=base_url,
        allowed_hosts=allowed_hosts if allowed_hosts is not None else ["standardebooks.org"],
        trust=overrides.pop("trust", 0.9),
        trusted_for_content_rights=overrides.pop("trusted_for_content_rights", True),
        requests_per_minute=overrides.pop("requests_per_minute", 6000.0),
        max_download_bytes=overrides.pop("max_download_bytes", 32 * 1024 * 1024),
        allow_http=overrides.pop("allow_http", False),
        languages=overrides.pop("languages", []),
        options=overrides.pop("options", {}),
        notes=overrides.pop("notes", ""),
        # Always a temporary directory. A cwd-relative default once let
        # the offline suite write its fixture catalogue into the real
        # data/corpus/manifests/, which a later live run then read back
        # as though it were the provider's own catalogue.
        cache_dir=overrides.pop("cache_dir", None) or Path(tempfile.mkdtemp(prefix="fh-corpus-test-")),
    )


def make_fetcher(mapping: dict[str, bytes | Path], **kwargs) -> FixtureFetcher:
    return FixtureFetcher(mapping, **kwargs)


# --------------------------------------------------------------------------
# EPUB builder
# --------------------------------------------------------------------------

_CONTAINER_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _chapter_xhtml(heading: str, paragraphs: list[str]) -> bytes:
    body = "\n".join(f"    <p>{p}</p>" for p in paragraphs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        f"  <head><title>{heading}</title></head>\n"
        f"  <body>\n    <h2>{heading}</h2>\n{body}\n  </body>\n</html>\n"
    ).encode()


def build_epub(
    *,
    title: str = "An Ordered Book",
    chapters: list[tuple[str, list[str]]] | None = None,
    reverse_spine: bool = False,
    include_zip_slip: bool = False,
    bomb_member: bool = False,
) -> bytes:
    """
    A valid, minimal EPUB.

    `reverse_spine` writes the zip members in the opposite order from the
    spine, which is how the "reading order, not filename order" test
    proves the normalizer follows the spine: a normalizer that iterated
    the archive would produce the chapters backwards.

    `include_zip_slip` adds a `../../../etc/evil.xhtml` member, and
    `bomb_member` adds a highly-compressible member with a huge expansion
    ratio -- both to prove the archive guards fire.
    """
    chapters = chapters or [
        ("Chapter One", ["The first chapter says alpha and it comes first in reading order."]),
        ("Chapter Two", ["The second chapter says beta and it comes second in reading order."]),
        ("Chapter Three", ["The third chapter says gamma and it comes last in reading order."]),
    ]

    names = [f"chapter{i + 1}.xhtml" for i in range(len(chapters))]

    manifest_items = "\n".join(
        f'    <item id="ch{i + 1}" href="{name}" media-type="application/xhtml+xml"/>'
        for i, name in enumerate(names)
    )
    spine_items = "\n".join(f'    <itemref idref="ch{i + 1}"/>' for i in range(len(names)))

    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"    <dc:title>{title}</dc:title>\n"
        '    <dc:identifier id="bookid">urn:uuid:test-epub</dc:identifier>\n'
        "    <dc:language>en</dc:language>\n"
        "  </metadata>\n"
        f"  <manifest>\n{manifest_items}\n  </manifest>\n"
        f"  <spine>\n{spine_items}\n  </spine>\n"
        "</package>\n"
    ).encode()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # The mimetype member must be first and stored uncompressed for
        # the EPUB magic-number sniff to find it in the first 200 bytes.
        archive.writestr(
            zipfile.ZipInfo("mimetype"), b"application/epub+zip", compress_type=zipfile.ZIP_STORED
        )
        archive.writestr("META-INF/container.xml", _CONTAINER_XML)
        archive.writestr("OEBPS/content.opf", opf)

        order = list(range(len(chapters)))
        if reverse_spine:
            order.reverse()
        for index in order:
            heading, paragraphs = chapters[index]
            archive.writestr(f"OEBPS/{names[index]}", _chapter_xhtml(heading, paragraphs))

        if include_zip_slip:
            archive.writestr("../../../etc/evil.xhtml", b"<html><body>escaped</body></html>")
        if bomb_member:
            # 8 MB of one repeated byte: compresses to a few kilobytes, so
            # the ratio is far past the guard and the size is past the
            # 1 MB floor the guard requires before it fires.
            archive.writestr("OEBPS/bomb.xhtml", b"A" * (8 * 1024 * 1024))

    return buffer.getvalue()


# --------------------------------------------------------------------------
# Multistream dump builder
# --------------------------------------------------------------------------


def build_multistream_dump(streams: list[list[tuple[int, str, str]]]) -> tuple[bytes, str]:
    """
    A real multistream bzip2 dump and its matching index.

    Returns `(data, index_text)`. Each inner list is one stream, given as
    `(page_id, title, page_xml)` triples; every stream is compressed
    independently and the results are concatenated, which is exactly what
    Wikimedia's multistream files are. The index is generated from the
    actual byte offsets, so a test that Range-fetches `index[i].offset`
    really does land on a decompressible stream boundary.

    Built here rather than committed because the offsets must agree with
    the bytes: a checked-in pair would silently rot the moment either
    side changed, and the failure would look like a parser bug.
    """
    chunks: list[bytes] = []
    index_lines: list[str] = []
    offset = 0
    for pages in streams:
        body = "".join(xml for _, _, xml in pages).encode("utf-8")
        compressed = bz2.compress(body)
        for page_id, title, _ in pages:
            index_lines.append(f"{offset}:{page_id}:{title}")
        chunks.append(compressed)
        offset += len(compressed)
    return b"".join(chunks), "\n".join(index_lines) + "\n"


def split_mediawiki_pages(xml: bytes) -> list[tuple[int, str, str]]:
    """
    Break a `<mediawiki>` document into `(page_id, title, page_xml)`
    triples, so the real captured fixture can be repackaged as a
    multistream dump without hand-copying its pages.
    """
    text = xml.decode("utf-8")
    out: list[tuple[int, str, str]] = []
    for match in re.finditer(r"[ \t]*<page>.*?</page>\n?", text, re.S):
        page_xml = match.group(0)
        title = re.search(r"<title>(.*?)</title>", page_xml, re.S).group(1)
        page_id = int(re.search(r"</title>\s*<ns>\d+</ns>\s*<id>(\d+)</id>", page_xml, re.S).group(1))
        out.append((page_id, title, page_xml))
    return out


def build_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# PDF builder
# --------------------------------------------------------------------------


def build_pdf(lines: list[str], *, with_text_layer: bool = True) -> bytes:
    """
    A minimal but genuinely valid PDF with an uncompressed content stream.

    Hand-built rather than library-generated so the fixture has no
    dependency and so the test for "PDF with a usable text layer" is
    exercising real `Tj` operators. `with_text_layer=False` produces a
    structurally valid PDF whose page has no text-showing operator at all
    -- the scanned-page case, which must be rejected rather than OCR'd.
    """
    if with_text_layer:
        shown = "\n".join(
            f"BT /F1 12 Tf 72 {700 - index * 18} Td ({_escape_pdf(line)}) Tj ET"
            for index, line in enumerate(lines)
        )
        content = f"{shown}\n".encode("latin-1")
    else:
        # A drawing operation only: valid content, no text layer.
        content = b"1 0 0 RG\n72 700 m 400 700 l S\n"

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")

    return bytes(out)


def _escape_pdf(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


__all__ = [
    "FIXTURES",
    "build_epub",
    "build_multistream_dump",
    "build_pdf",
    "build_zip",
    "fixture_bytes",
    "fixture_text",
    "make_config",
    "make_fetcher",
    "make_policy",
    "make_source",
    "split_mediawiki_pages",
]
