"""
Content-addressed storage for harvested artifacts.

Layout under `data/corpus/`:

    blobs/<aa>/<bb>/<sha256>          immutable raw downloads
    normalized/<aa>/<bb>/<sha256>.txt normalized UTF-8 text
    licenses/<aa>/<bb>/<sha256>.txt   licence text preserved as evidence
    metadata/<document_id>.json       full per-document record
    quarantine/<document_id>.json     rights-quarantined dossiers
    locks/                            run lockfiles
    reports/                          per-run reports
    manifests/                        materialization manifests

Content addressing means two sources offering the same file cost one
blob, and it makes the raw download *immutable by construction*: a
changed file has a different hash and therefore a different path, so the
original can never be silently overwritten.

Every path built here passes through `safe_filename` and `ensure_within`
before anything is opened. Document ids and hashes are the only things
that ever become path components, and both are generated, not harvested.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .security import ensure_within, safe_filename, sha256_bytes

logger = logging.getLogger(__name__)

ARTIFACT_RAW = "raw"
ARTIFACT_NORMALIZED = "normalized"
ARTIFACT_LICENSE = "license"
ARTIFACT_METADATA = "metadata"


def mint_document_id(source_id: str, external_id: str) -> str:
    """
    A stable document id derived from (source, external id) alone.

    Deliberately *not* content-derived: re-downloading a document whose
    file changed upstream must still address the same document, or every
    upstream correction would fork a new document and orphan the old
    one's provenance. Content identity is tracked separately by the
    sha256 columns.
    """
    digest = hashlib.sha256(f"{source_id}|{external_id}".encode()).hexdigest()[:20]
    prefix = safe_filename(source_id, max_length=24, default="src").lower()
    return f"{prefix}_{digest}"


@dataclass
class CorpusStorage:
    """Filesystem layout manager. Creates directories lazily, never eagerly."""

    root: Path

    @property
    def blobs(self) -> Path:
        return self.root / "blobs"

    @property
    def normalized(self) -> Path:
        return self.root / "normalized"

    @property
    def licenses(self) -> Path:
        return self.root / "licenses"

    @property
    def metadata(self) -> Path:
        return self.root / "metadata"

    @property
    def quarantine(self) -> Path:
        return self.root / "quarantine"

    @property
    def locks(self) -> Path:
        return self.root / "locks"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def manifests(self) -> Path:
        return self.root / "manifests"

    def ensure_layout(self) -> None:
        for directory in (
            self.blobs, self.normalized, self.licenses, self.metadata,
            self.quarantine, self.locks, self.reports, self.manifests,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------- content paths

    def _sharded(self, base: Path, digest: str, suffix: str = "") -> Path:
        """
        Two levels of 2-hex-character sharding.

        A flat directory of a million blobs is slow on every filesystem
        and unpleasant on most; 256x256 shards keep any one directory at
        a few dozen entries even for a large archive-profile corpus.
        """
        if len(digest) < 4 or not all(c in "0123456789abcdef" for c in digest.lower()):
            raise ValueError(f"Refusing to build a path from a non-hex digest: {digest!r}")
        digest = digest.lower()
        return base / digest[:2] / digest[2:4] / f"{digest}{suffix}"

    def blob_path(self, digest: str) -> Path:
        return self._sharded(self.blobs, digest)

    def normalized_path(self, digest: str) -> Path:
        return self._sharded(self.normalized, digest, ".txt")

    def license_path(self, digest: str) -> Path:
        return self._sharded(self.licenses, digest, ".txt")

    def metadata_path(self, document_id: str) -> Path:
        return self.metadata / f"{safe_filename(document_id)}.json"

    def quarantine_path(self, document_id: str) -> Path:
        return self.quarantine / f"{safe_filename(document_id)}.json"

    # ------------------------------------------------------------- writing

    def store_blob(self, data: bytes, *, digest: str = "") -> tuple[str, Path, bool]:
        """
        Store raw bytes. Returns (sha256, path, was_new).

        An existing blob is never rewritten -- identical content is
        identical, and rewriting would only risk truncating a good file
        if the process died mid-write. `was_new` is what lets the caller
        distinguish a genuine download from a deduplicated one.
        """
        digest = digest or sha256_bytes(data)
        path = self.blob_path(digest)
        ensure_within(self.blobs, path)
        if path.exists() and path.stat().st_size == len(data):
            return digest, path, False
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(path, data)
        return digest, path, True

    def store_normalized(self, text: str, *, digest: str = "") -> tuple[str, Path, bool]:
        data = text.encode("utf-8")
        digest = digest or hashlib.sha256(data).hexdigest()
        path = self.normalized_path(digest)
        ensure_within(self.normalized, path)
        if path.exists():
            return digest, path, False
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(path, data)
        return digest, path, True

    def store_license(self, text: str) -> tuple[str, Path]:
        """
        Preserve licence text as its own artifact.

        This is why boilerplate stripping is safe: the licence is removed
        from the indexed body only after it has been written here, so no
        rights information is lost by normalization.
        """
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        path = self.license_path(digest)
        ensure_within(self.licenses, path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(path, data)
        return digest, path

    def write_metadata(self, document_id: str, payload: dict) -> Path:
        path = self.metadata_path(document_id)
        ensure_within(self.metadata, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
        return path

    def write_quarantine(self, document_id: str, payload: dict) -> Path:
        """
        A quarantine dossier: everything known about why a document was
        held. The file itself is *not* deleted from blobs -- rule: "ne pas
        supprimer silencieusement les fichiers" -- it simply never reaches
        materialization or the index.
        """
        path = self.quarantine_path(document_id)
        ensure_within(self.quarantine, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
        return path

    def read_normalized(self, digest: str) -> str:
        return self.normalized_path(digest).read_text(encoding="utf-8")

    def read_blob(self, digest: str) -> bytes:
        return self.blob_path(digest).read_bytes()

    # ---------------------------------------------------------- materialize

    def link_or_copy(self, source: Path, destination: Path) -> str:
        """
        Materialize a blob into the ingested corpus tree.

        A hardlink when the filesystem allows it (no second copy of a
        50 MB text), a real copy otherwise. Returns "hardlink" or "copy"
        so the manifest records which happened. A symlink is deliberately
        never used: a symlink into the content-addressed store would break
        the moment the store moved, and a silently broken link in the
        ingest path is exactly the failure the brief forbids.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        try:
            os.link(source, destination)
            return "hardlink"
        except OSError as exc:
            logger.debug("Hardlink %s -> %s failed (%s); copying instead", source, destination, exc)
            shutil.copy2(source, destination)
            return "copy"

    # --------------------------------------------------------------- disk

    def usage_bytes(self) -> int:
        total = 0
        for directory in (self.blobs, self.normalized, self.licenses):
            if not directory.exists():
                continue
            for path in directory.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
        return total


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """
    Write via a temporary file in the same directory, then rename.

    A partially-written blob whose name is its own content hash would be
    a permanent, silent corruption -- every future run would see the path
    exists and skip it. `os.replace` is atomic within a filesystem, so
    the file either exists complete or does not exist.
    """
    temp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        if temp.exists():
            with contextlib.suppress(OSError):
                temp.unlink()


def free_disk_bytes(path: Path) -> int:
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    usage = shutil.disk_usage(target)
    return usage.free


def suggest_budget(path: Path) -> dict:
    """
    Propose a conservative storage budget from free disk space.

    Deliberately timid: a quarter of what is free, capped at 20 GB, and
    a floor of 5 GB kept permanently free. The brief's rule is "ne jamais
    consommer le dernier espace disponible", and this is a *suggestion*
    the operator must copy into configuration -- `corpus sync` refuses to
    run on a suggestion (see scheduler.py).
    """
    free = free_disk_bytes(path)
    suggested_total = min(int(free * 0.25), 20 * 1024**3)
    return {
        "free_bytes": free,
        "max_total_corpus_bytes": max(1024**3, suggested_total),
        "min_free_disk_bytes": max(5 * 1024**3, int(free * 0.15)),
        "max_download_bytes_per_run": max(256 * 1024**2, min(2 * 1024**3, suggested_total // 8)),
    }


def human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TB"


__all__ = [
    "ARTIFACT_LICENSE",
    "ARTIFACT_METADATA",
    "ARTIFACT_NORMALIZED",
    "ARTIFACT_RAW",
    "CorpusStorage",
    "free_disk_bytes",
    "human_bytes",
    "mint_document_id",
    "suggest_budget",
]
