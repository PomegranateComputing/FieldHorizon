"""
Bulk dump acquisition: resolution, resumable download, and multistream
random access.

This is transport-neutral machinery. `adapters/wikisource_dump.py` uses
it against the Wikimedia XML content-file exports; nothing here knows
what a Wikisource page is.

**Why random access matters.** `frwikisource-pages-articles-multistream`
is 2.8 GB. The brief's `dump-seed` mode is bounded to 20-50 works, and
downloading 2.8 GB to read fifty pages would be both absurd and rude to
an institution giving the file away for free. A multistream dump is a
concatenation of independently-decompressible bzip2 streams of 100 pages
each, and the companion index says which byte offset holds which page.
So fifty works cost the 28 MB index plus a handful of HTTP Range
requests -- roughly 1-2 MB of the 2.8 GB. Verified against the live
service on 2026-08-01: a Range request for one stream decompresses
standalone into exactly 100 `<page>` elements.

**Everything published is used as published.** The dump files, their
`dumpstatus.json` manifest, and their SHA-1 checksums are what the
service offers for exactly this purpose. There is no gate here to step
around, no robots.txt to disregard (the fetcher enforces it regardless),
and no rate limit to outrun.
"""

from __future__ import annotations

import bz2
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .security import SecurityError, ensure_within

logger = logging.getLogger(__name__)


class DumpError(RuntimeError):
    """A dump could not be resolved, downloaded, or verified."""


class ChecksumMismatch(DumpError):
    """
    The bytes on disk are not the bytes the manifest describes.

    Always fatal, never retried in place: a resumed `.part` whose prefix
    is wrong produces a file that is corrupt in the middle, and appending
    more bytes to it will never help.
    """


# --------------------------------------------------------------------------
# Release resolution
# --------------------------------------------------------------------------

#: `YYYYMMDD/` entries in the wiki's directory index. Anchored so that a
#: link to `latest/` -- a symlink whose target moves under us mid-run --
#: is not mistaken for a dated release.
_DATE_DIR_RE = re.compile(r'href="(\d{8})/"')


def parse_dump_dates(html: str) -> list[str]:
    """
    Dated releases, newest last.

    Deliberately NOT a general HTML parse: one anchored pattern against
    one official directory index, extracting nothing but eight digits.
    `latest/` is excluded on purpose -- it is a moving symlink, and a run
    that resolves it twice can silently straddle two different dumps.
    """
    return sorted(set(_DATE_DIR_RE.findall(html or "")))


def latest_complete_date(dates: list[str], *, not_after: str = "") -> str:
    """
    The newest dated release, optionally pinned to a ceiling.

    `not_after` exists so a run can be reproduced: "the dump as of
    2026-07-01" is a question an audit will ask, and answering it must
    not require that no newer dump has appeared since.
    """
    usable = [d for d in dates if not not_after or d <= not_after]
    if not usable:
        raise DumpError(f"no dump release found (dates={dates[-3:]}, not_after={not_after!r})")
    return usable[-1]


@dataclass(frozen=True)
class DumpFile:
    """One file in a release, with the checksums the manifest published."""

    name: str
    url: str
    size: int = 0
    sha1: str = ""
    md5: str = ""

    def absolute_url(self, base: str) -> str:
        if self.url.startswith(("http://", "https://")):
            return self.url
        return f"{base.rstrip('/')}/{self.url.lstrip('/')}"


@dataclass(frozen=True)
class DumpRelease:
    wiki: str
    date: str
    files: dict[str, DumpFile] = field(default_factory=dict)

    def find(self, *fragments: str) -> DumpFile:
        """
        The single file whose name contains every fragment.

        Ambiguity raises rather than picking one. The sharded jobs
        publish names differing only by a page range, and quietly
        harvesting shard 3 of 8 because it sorted first would look like a
        successful run that lost seven eighths of the corpus.
        """
        matches = [f for name, f in sorted(self.files.items()) if all(x in name for x in fragments)]
        if not matches:
            raise DumpError(f"{self.wiki} {self.date}: no file matching {fragments}")
        if len(matches) > 1:
            raise DumpError(
                f"{self.wiki} {self.date}: {len(matches)} files match {fragments}: "
                f"{', '.join(m.name for m in matches[:4])}"
            )
        return matches[0]


def parse_dumpstatus(payload: bytes, *, wiki: str, date: str) -> DumpRelease:
    """
    `dumpstatus.json`, restricted to jobs the service reports as done.

    A job that is `waiting`, `in-progress`, or `failed` has files that
    either do not exist or are still being written. Reading one produces
    a truncated corpus with no error anywhere -- so an unfinished job is
    dropped here, at the only place that can still tell the difference.
    """
    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise DumpError(f"{wiki} {date}: dumpstatus.json is not JSON ({exc})") from exc

    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        raise DumpError(f"{wiki} {date}: dumpstatus.json has no jobs object")

    files: dict[str, DumpFile] = {}
    skipped: list[str] = []
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if job.get("status") != "done":
            skipped.append(f"{job_name}={job.get('status')}")
            continue
        for name, entry in (job.get("files") or {}).items():
            if not isinstance(entry, dict):
                continue
            files[name] = DumpFile(
                name=name,
                url=str(entry.get("url", "")),
                size=int(entry.get("size", 0) or 0),
                sha1=str(entry.get("sha1", "")),
                md5=str(entry.get("md5", "")),
            )
    if skipped:
        logger.info("%s %s: skipped %d unfinished job(s): %s",
                    wiki, date, len(skipped), ", ".join(skipped[:5]))
    if not files:
        raise DumpError(f"{wiki} {date}: no completed job published any file")
    return DumpRelease(wiki=wiki, date=date, files=files)


# --------------------------------------------------------------------------
# Resumable download
# --------------------------------------------------------------------------

#: Read/write chunk. Large enough that hashing is not syscall-bound,
#: small enough that a cancelled run loses a trivial amount of work.
CHUNK_BYTES = 1024 * 1024


@dataclass
class DownloadState:
    """Where a resumable download got to."""

    path: Path
    part_path: Path
    downloaded_bytes: int = 0
    expected_bytes: int = 0
    expected_sha1: str = ""
    complete: bool = False
    verified: bool = False

    @property
    def remaining(self) -> int:
        if not self.expected_bytes:
            return 0
        return max(0, self.expected_bytes - self.downloaded_bytes)


def download_state(destination: Path, dump_file: DumpFile) -> DownloadState:
    """
    Inspect the filesystem for an interrupted or finished download.

    The `.part` suffix is what makes resumption safe: a complete file and
    a half-written one are never the same path, so nothing downstream can
    read a truncated dump believing it whole.
    """
    part = destination.with_suffix(destination.suffix + ".part")
    state = DownloadState(
        path=destination,
        part_path=part,
        expected_bytes=dump_file.size,
        expected_sha1=dump_file.sha1,
    )
    if destination.exists():
        state.downloaded_bytes = destination.stat().st_size
        state.complete = True
    elif part.exists():
        state.downloaded_bytes = part.stat().st_size
        if dump_file.size and state.downloaded_bytes > dump_file.size:
            # Longer than the manifest says: not a resumable prefix of
            # anything. Truncating to the expected length would leave a
            # file whose tail is from a different release, which the
            # checksum would catch but only after a multi-gigabyte
            # download. Start over instead.
            logger.warning(
                "%s: partial file is %d bytes but the release declares %d; discarding it",
                part.name, state.downloaded_bytes, dump_file.size,
            )
            part.unlink()
            state.downloaded_bytes = 0
    return state


def verify_sha1(path: Path, expected: str) -> str:
    """
    Hash a file in chunks and compare.

    Streamed rather than read whole: these files reach several gigabytes,
    and `path.read_bytes()` on one is how a harvester gets killed by the
    OOM reaper at the very last step of a long download.
    """
    digest = hashlib.sha1()  # noqa: S324 -- the service publishes SHA-1; we verify what it published
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    actual = digest.hexdigest()
    if expected and actual != expected:
        raise ChecksumMismatch(
            f"{path.name}: sha1 {actual} does not match the published {expected}"
        )
    return actual


def finalize_download(state: DownloadState) -> Path:
    """
    Verify the `.part` and move it into place.

    Verification happens BEFORE the rename, so a file that fails its
    checksum never appears under its real name even for an instant. A
    mismatched part is deleted: it cannot be resumed, because whatever is
    wrong with it is somewhere in the bytes already written.
    """
    if state.path.exists():
        state.complete = True
        return state.path
    if not state.part_path.exists():
        raise DumpError(f"{state.part_path.name}: nothing downloaded")

    if state.expected_bytes and state.part_path.stat().st_size != state.expected_bytes:
        raise DumpError(
            f"{state.part_path.name}: {state.part_path.stat().st_size} bytes, "
            f"expected {state.expected_bytes}"
        )
    if state.expected_sha1:
        try:
            verify_sha1(state.part_path, state.expected_sha1)
        except ChecksumMismatch:
            state.part_path.unlink()
            raise
        state.verified = True

    state.part_path.replace(state.path)
    state.complete = True
    return state.path


# --------------------------------------------------------------------------
# Multistream index
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexEntry:
    """One line of the multistream index: `offset:page_id:title`."""

    offset: int
    page_id: int
    title: str


def parse_multistream_index(text: str) -> list[IndexEntry]:
    """
    The `offset:page_id:title` index.

    Split at most twice. A title may itself contain colons -- the live
    French index contains `Les Fougerêts : Patrimoine et identité d’une
    commune de Haute-Bretagne` -- and an unbounded split would truncate
    that title to `Les Fougerêts ` and, worse, hand the rest to whatever
    read the next field.
    """
    entries: list[IndexEntry] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        offset, page_id, title = parts
        try:
            entries.append(IndexEntry(int(offset), int(page_id), title))
        except ValueError:
            continue
    return entries


#: Namespace prefixes as the French and English Wikisources write them.
#: Used ONLY to explain a title, never to decide one: the authority is
#: the `<ns>` element inside the dump, which is unambiguous. A title is
#: allowed to contain a colon without being namespaced, so prefix
#: matching alone would quietly discard real works.
KNOWN_NAMESPACE_PREFIXES = frozenset(
    {
        "Page", "Auteur", "Author", "Catégorie", "Category", "Modèle", "Template",
        "Fichier", "File", "Image", "Wikisource", "MediaWiki", "Portail", "Portal",
        "Aide", "Help", "Livre", "Index", "Discussion", "Talk", "Utilisateur", "User",
        "Transwiki", "Module", "TimedText", "Gadget", "Traduction", "Translation",
        "Référence", "Special", "Spécial",
    }
)


def looks_namespaced(title: str) -> bool:
    """
    Whether a title carries a known namespace prefix.

    A heuristic, and labelled as one. `Les Fougerêts : Patrimoine et
    identité d’une commune de Haute-Bretagne` is a main-namespace work
    whose title contains a colon; splitting on the colon would file it
    under a namespace called "Les Fougerêts " and drop it.
    """
    head, separator, _ = title.partition(":")
    return bool(separator) and head.strip() in KNOWN_NAMESPACE_PREFIXES


@dataclass(frozen=True)
class StreamRange:
    """One independently-decompressible bzip2 stream, as a byte range."""

    offset: int
    end: int
    entries: tuple[IndexEntry, ...]

    @property
    def length(self) -> int:
        return self.end - self.offset + 1

    def header(self) -> str:
        """An HTTP Range header value for exactly this stream."""
        return f"bytes={self.offset}-{self.end}"


def stream_ranges(entries: list[IndexEntry], *, total_bytes: int = 0) -> list[StreamRange]:
    """
    Group index entries into byte ranges, one per bzip2 stream.

    A stream runs from its own offset to one byte before the next
    distinct offset. The last stream has no successor, so it runs to the
    end of the file -- and if the file size is unknown, it is dropped
    rather than guessed: an open-ended Range against a 2.8 GB file is
    precisely the unbounded download this whole module exists to avoid.
    """
    by_offset: dict[int, list[IndexEntry]] = {}
    for entry in entries:
        by_offset.setdefault(entry.offset, []).append(entry)

    offsets = sorted(by_offset)
    ranges: list[StreamRange] = []
    for index, offset in enumerate(offsets):
        if index + 1 < len(offsets):
            end = offsets[index + 1] - 1
        elif total_bytes:
            end = total_bytes - 1
        else:
            logger.info("dropping the final stream at offset %d: file size unknown", offset)
            continue
        ranges.append(StreamRange(offset, end, tuple(by_offset[offset])))
    return ranges


# --------------------------------------------------------------------------
# Decompression
# --------------------------------------------------------------------------

#: A single stream holds 100 pages. 64 MB is far above any real one and
#: far below anything that threatens the process.
MAX_STREAM_DECOMPRESSED_BYTES = 64 * 1024 * 1024


def decompress_prefix(data: bytes, *, max_bytes: int = MAX_STREAM_DECOMPRESSED_BYTES) -> bytes:
    """
    Decompress as much of a truncated bzip2 file as the bytes allow.

    For reading the *beginning* of something far too large to hold. The
    frwikisource multistream index is 28 MB compressed and expands past
    200 MB -- and `dump-seed` needs its first few hundred lines. Fetching
    a 512 kB prefix and decompressing what is there costs a thousandth of
    the bytes and answers the same question.

    Unlike `decompress_stream` this tolerates an incomplete final stream:
    truncation is the expected condition, not an error. The last line is
    usually cut mid-title, and `parse_multistream_index` drops malformed
    lines, so it simply does not become an entry.
    """
    decompressor = bz2.BZ2Decompressor()
    chunks: list[bytes] = []
    total = 0
    for start in range(0, len(data), CHUNK_BYTES):
        try:
            block = decompressor.decompress(data[start:start + CHUNK_BYTES], max_length=CHUNK_BYTES)
        except (OSError, EOFError, ValueError):
            # A stream boundary cut mid-block. Everything before it is
            # still good, and that is what was wanted.
            break
        while block:
            total += len(block)
            if total > max_bytes:
                return b"".join(chunks)
            chunks.append(block)
            if decompressor.eof or decompressor.needs_input:
                break
            try:
                block = decompressor.decompress(b"", max_length=CHUNK_BYTES)
            except (OSError, EOFError, ValueError):
                block = b""
        if decompressor.eof:
            break
    return b"".join(chunks)


def decompress_stream(data: bytes, *, max_bytes: int = MAX_STREAM_DECOMPRESSED_BYTES) -> bytes:
    """
    Decompress one bzip2 stream, bounded.

    Bounded during decompression rather than checked after: a compression
    bomb's whole point is that its expanded size is discovered too late,
    and `bz2.decompress()` on one exhausts memory before returning
    anything to check.
    """
    decompressor = bz2.BZ2Decompressor()
    chunks: list[bytes] = []
    total = 0
    view = memoryview(data)
    position = 0
    while position < len(view):
        block = decompressor.decompress(
            view[position:position + CHUNK_BYTES], max_length=CHUNK_BYTES
        )
        position += CHUNK_BYTES
        while True:
            if block:
                total += len(block)
                if total > max_bytes:
                    raise SecurityError(
                        f"bzip2 stream expands past the {max_bytes}-byte limit"
                    )
                chunks.append(block)
            if decompressor.eof or decompressor.needs_input:
                break
            block = decompressor.decompress(b"", max_length=CHUNK_BYTES)
        if decompressor.eof:
            break
    return b"".join(chunks)


#: A stream fragment is a run of `<page>` elements with no root element
#: and no XML declaration -- it is the middle of a document. Wrapping it
#: is what makes it parseable. The wrapper is ours and contains no
#: DOCTYPE or entity declaration; `safe_parse_xml` still refuses any the
#: fragment might carry.
_STREAM_WRAPPER = '<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">\n{0}\n</mediawiki>'


def wrap_stream_fragment(fragment: bytes | str) -> bytes:
    """Make a mid-document run of `<page>` elements into a parseable document."""
    text = fragment.decode("utf-8", errors="replace") if isinstance(fragment, bytes) else fragment
    stripped = text.strip()
    if stripped.startswith("<mediawiki") or stripped.startswith("<?xml"):
        return text.encode("utf-8")
    # The final stream of a dump is the closing tag alone.
    stripped = stripped.removesuffix("</mediawiki>").strip()
    return _STREAM_WRAPPER.format(stripped).encode("utf-8")


def checkpoint_token(offset: int, page_id: int) -> str:
    """
    An opaque resumption point: the stream offset plus the last page id
    inside it. Both are needed -- the offset alone restarts a stream from
    its first page and re-imports up to 99 documents, and the page id
    alone does not say which stream to seek to.
    """
    return f"{offset}:{page_id}"


def parse_checkpoint(token: str) -> tuple[int, int]:
    if not token:
        return 0, 0
    offset, _, page_id = token.partition(":")
    try:
        return int(offset), int(page_id or 0)
    except ValueError:
        logger.warning("unreadable checkpoint %r; restarting this dump from the beginning", token)
        return 0, 0


def dump_destination(root: Path, wiki: str, date: str, filename: str) -> Path:
    """
    Where a dump file lands, with the path confined to `root`.

    `filename` comes from a manifest, which is provider data, and
    provider data is never trusted with a path. `ensure_within` is what
    stops a name like `../../etc/cron.d/harvest` from being written
    anywhere at all.
    """
    directory = root / "dumps" / wiki / date
    target = (directory / Path(filename).name).resolve()
    return ensure_within(root.resolve(), target)


__all__ = [
    "CHUNK_BYTES",
    "KNOWN_NAMESPACE_PREFIXES",
    "MAX_STREAM_DECOMPRESSED_BYTES",
    "ChecksumMismatch",
    "DownloadState",
    "DumpError",
    "DumpFile",
    "DumpRelease",
    "IndexEntry",
    "StreamRange",
    "checkpoint_token",
    "decompress_stream",
    "download_state",
    "dump_destination",
    "finalize_download",
    "latest_complete_date",
    "looks_namespaced",
    "parse_checkpoint",
    "parse_dump_dates",
    "parse_dumpstatus",
    "parse_multistream_index",
    "stream_ranges",
    "verify_sha1",
    "wrap_stream_fragment",
]
