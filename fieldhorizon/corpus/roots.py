"""
Absolute data roots, and the guard that keeps tests out of them.

Defect #8 of the eleven the Phase I pilot found was the worst kind — a
test artefact reaching production:

> A cache path resolved against the **working directory**, so the offline
> test suite wrote its fixture catalogue into the real
> `data/corpus/manifests/`, and a live run then labelled a real document
> with a fixture's title.

Phase I fixed that one path and added a regression test. This module
addresses the *class*:

* Every data root is resolved once, absolutely, and recorded here.
* `resolve()` refuses a relative path outright rather than silently
  making it relative to `os.getcwd()`.
* The resolved roots are logged at startup, so "where is it writing"
  never requires reading code.
* `install_test_guard()` makes any write under the production root raise
  — which is what turns "the tests should not touch real data" from an
  intention into a mechanism.

The root can be overridden with `$FIELDHORIZON_DATA_ROOT`. That exists
for deployments that separate code from data; it is NOT a convenience for
tests, which get a temporary directory and the guard.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_ROOT_ENV = "FIELDHORIZON_DATA_ROOT"


class PathEscapeError(RuntimeError):
    """A path was built that escapes, or does not derive from, a data root."""


class ProductionWriteBlocked(RuntimeError):
    """
    A test tried to write inside the production data root.

    Raised by the guard installed by `install_test_guard()`. Seeing this
    in a test failure means the test is about to do what defect #8 did.
    """


@dataclass(frozen=True)
class DataRoots:
    """
    Every directory the harvester may write to, resolved absolutely.

    Constructed from `AppConfig`, which already resolves its own paths
    relative to the config file's directory rather than the working
    directory. This type exists so that the *set* of roots is nameable,
    auditable, and loggable, rather than being implied by whichever
    module happened to build a path.
    """

    root: Path
    data: Path
    corpus: Path
    blobs: Path
    normalized: Path
    licenses: Path
    metadata: Path
    quarantine: Path
    locks: Path
    reports: Path
    manifests: Path
    dumps: Path
    books: Path
    manifestos: Path
    outputs: Path
    logs: Path
    database: Path

    @classmethod
    def from_config(cls, cfg) -> DataRoots:
        """
        Resolve every root from an `AppConfig`.

        `$FIELDHORIZON_DATA_ROOT` overrides the *data* subtree only. The
        config file, the ontology, and the source YAML stay where the
        config says they are: separating data from code is the point, and
        moving the code with it would defeat it.
        """
        root = Path(cfg.root).resolve()

        override = os.environ.get(DATA_ROOT_ENV, "").strip()
        if override:
            data = Path(override).expanduser()
            if not data.is_absolute():
                raise PathEscapeError(
                    f"${DATA_ROOT_ENV} must be an absolute path; got {override!r}. "
                    f"A relative data root resolves against the working directory, "
                    f"which is exactly the defect this guard exists to prevent."
                )
            data = data.resolve()
        else:
            data = (root / "data").resolve()

        corpus = data / "corpus"
        return cls(
            root=root,
            data=data,
            corpus=corpus,
            blobs=corpus / "blobs",
            normalized=corpus / "normalized",
            licenses=corpus / "licenses",
            metadata=corpus / "metadata",
            quarantine=corpus / "quarantine",
            locks=corpus / "locks",
            reports=corpus / "reports",
            manifests=corpus / "manifests",
            dumps=corpus / "dumps",
            books=Path(cfg.books).resolve(),
            manifestos=Path(cfg.manifestos).resolve(),
            outputs=Path(cfg.outputs).resolve(),
            logs=Path(cfg.logs).resolve(),
            database=Path(cfg.database).resolve(),
        )

    def all_roots(self) -> dict[str, Path]:
        return {
            name: getattr(self, name)
            for name in (
                "root", "data", "corpus", "blobs", "normalized", "licenses", "metadata",
                "quarantine", "locks", "reports", "manifests", "dumps", "books",
                "manifestos", "outputs", "logs", "database",
            )
        }

    def log_resolved(self, *, level: int = logging.INFO) -> None:
        """
        Record every resolved root.

        Called once at harvester startup. "Where is it writing" should
        never require reading code, and a root that has silently moved
        should be visible in the first lines of a run log.
        """
        logger.log(level, "Field Horizon data roots (all absolute):")
        for name, path in self.all_roots().items():
            marker = "" if path.is_absolute() else "  <-- NOT ABSOLUTE"
            logger.log(level, "  %-11s %s%s", name, path, marker)
        if os.environ.get(DATA_ROOT_ENV):
            logger.log(level, "  (data subtree overridden by $%s)", DATA_ROOT_ENV)

    def ensure(self) -> None:
        for name, path in self.all_roots().items():
            if name == "database":
                path.parent.mkdir(parents=True, exist_ok=True)
            elif name != "root":
                path.mkdir(parents=True, exist_ok=True)

    def contains(self, path: Path | str) -> bool:
        """Whether `path` lies inside the data subtree."""
        candidate = Path(path).resolve()
        return candidate == self.data or self.data in candidate.parents

    def to_dict(self) -> dict:
        return {name: str(path) for name, path in self.all_roots().items()}


def resolve(base: Path, *parts: str) -> Path:
    """
    Join `parts` onto an absolute `base`, refusing to invent a base.

    The single rule this module exists to enforce: a data path must
    derive from a declared root. Passing a relative base raises rather
    than quietly resolving against `os.getcwd()` -- which is precisely
    how the fixture catalogue ended up in production data.
    """
    base = Path(base)
    if not base.is_absolute():
        raise PathEscapeError(
            f"Refusing to resolve {parts!r} against the relative base {base!r}. "
            f"A relative base resolves against the current working directory, "
            f"so the same code writes to different places depending on where it "
            f"was started."
        )

    target = base.joinpath(*parts).resolve()
    base_resolved = base.resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise PathEscapeError(f"{target} escapes its root {base_resolved}")
    return target


# --------------------------------------------------------------------------
# Test isolation
# --------------------------------------------------------------------------

#: Set by `install_test_guard`. Any attempt to open a path under this
#: root for writing raises. Module-level because the patch it installs is
#: process-wide -- there is no way to make this per-test and still catch
#: a write from library code three frames down.
_BLOCKED_ROOT: Path | None = None
_BLOCKED_DATABASE: Path | None = None
_ORIGINAL_OPEN = None
_ORIGINAL_CONNECT = None

_WRITE_MODES = frozenset({"w", "a", "x", "+"})


def _is_write_mode(mode: str) -> bool:
    return any(flag in mode for flag in _WRITE_MODES)


def install_test_guard(blocked_root: Path, *, blocked_database: Path | None = None) -> None:
    """
    Make any write under `blocked_root` raise, process-wide.

    Installed by the test suite against the *production* root. A test
    that writes there is doing what defect #8 did, and should fail
    loudly at the moment of the write rather than corrupt real data and
    surface later as a mislabelled document.

    Reads are left alone: tests legitimately read the repository's own
    config and fixtures. Only writes are blocked, and only under the one
    root.
    """
    global _BLOCKED_ROOT, _BLOCKED_DATABASE, _ORIGINAL_OPEN, _ORIGINAL_CONNECT

    import builtins
    import sqlite3

    _BLOCKED_ROOT = Path(blocked_root).resolve()
    _BLOCKED_DATABASE = Path(blocked_database).resolve() if blocked_database else None

    if _ORIGINAL_OPEN is None:
        _ORIGINAL_OPEN = builtins.open

        def guarded_open(file, mode="r", *args, **kwargs):
            if _is_write_mode(str(mode)):
                _refuse_if_blocked(file, "open() for writing")
            return _ORIGINAL_OPEN(file, mode, *args, **kwargs)

        builtins.open = guarded_open

    if _ORIGINAL_CONNECT is None:
        _ORIGINAL_CONNECT = sqlite3.connect

        def guarded_connect(database, *args, **kwargs):
            # The production database is blocked outright, read or write:
            # a test has no business opening it at all, and a read that
            # succeeds today becomes a write when someone extends the test.
            if _BLOCKED_DATABASE is not None and str(database) not in (":memory:", ""):
                try:
                    if Path(database).resolve() == _BLOCKED_DATABASE:
                        raise ProductionWriteBlocked(
                            f"Refusing to open the production database from a test: {database}"
                        )
                except OSError:
                    pass
            _refuse_if_blocked(database, "sqlite3.connect()")
            return _ORIGINAL_CONNECT(database, *args, **kwargs)

        sqlite3.connect = guarded_connect


def _refuse_if_blocked(target, operation: str) -> None:
    if _BLOCKED_ROOT is None:
        return
    try:
        path = Path(target)
    except TypeError:
        return  # a file descriptor, not a path
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        resolved = path.resolve()
    except OSError:
        return
    if resolved == _BLOCKED_ROOT or _BLOCKED_ROOT in resolved.parents:
        raise ProductionWriteBlocked(
            f"{operation} refused: {resolved} is inside the production data root "
            f"{_BLOCKED_ROOT}. Tests must write only to a temporary directory. "
            f"This is the guard for pilot defect #8, where the offline suite wrote "
            f"its fixture catalogue into real data and a live run then read it back."
        )


def remove_test_guard() -> None:
    """Restore the unpatched builtins. Used by the fixture's teardown."""
    global _BLOCKED_ROOT, _BLOCKED_DATABASE, _ORIGINAL_OPEN, _ORIGINAL_CONNECT

    import builtins
    import sqlite3

    if _ORIGINAL_OPEN is not None:
        builtins.open = _ORIGINAL_OPEN
        _ORIGINAL_OPEN = None
    if _ORIGINAL_CONNECT is not None:
        sqlite3.connect = _ORIGINAL_CONNECT
        _ORIGINAL_CONNECT = None
    _BLOCKED_ROOT = None
    _BLOCKED_DATABASE = None


def guard_is_active() -> bool:
    return _BLOCKED_ROOT is not None


__all__ = [
    "DATA_ROOT_ENV",
    "DataRoots",
    "PathEscapeError",
    "ProductionWriteBlocked",
    "guard_is_active",
    "install_test_guard",
    "remove_test_guard",
    "resolve",
]
