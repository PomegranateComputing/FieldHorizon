"""
Run gating: budgets, disk headroom, and the concurrency lock.

Three responsibilities, all of them about refusing to start rather than
about doing work.

**Budget enforcement.** `BudgetTracker` is consulted before every
download and after every one, and a run stops cleanly at its limit
rather than being killed. "Cleanly" matters: a stopped run leaves every
document in a resumable state, so the next run continues rather than
redoing.

**Disk safety.** Autonomous modes refuse to run without a configured
`max_total_corpus_bytes` and `min_free_disk_bytes`. This is the brief's
"refuser de lancer un mode autonome si aucun seuil de stockage
raisonnable n'est configuré", and it is a refusal, not a warning -- a
harvester with no storage ceiling will eventually fill the disk, and
doing so on someone's workstation is not an acceptable failure mode. A
suggested configuration is computed from free space and printed, but
never silently adopted.

**The lock.** A PID lockfile prevents two concurrent syncs. Stale locks
(the process is gone) are reclaimed automatically; live ones are
refused.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .policy import ACQUISITION_PROFILES, Budget
from .storage import free_disk_bytes, human_bytes, suggest_budget

logger = logging.getLogger(__name__)


class BudgetExhausted(Exception):
    """A run reached one of its limits. Not an error -- a clean stop."""

    def __init__(self, limit_name: str, message: str) -> None:
        super().__init__(message)
        self.limit_name = limit_name


class BudgetNotConfigured(RuntimeError):
    """Autonomous mode was requested with no storage ceiling configured."""


class LockHeld(RuntimeError):
    """Another harvester run is already in progress."""


@dataclass
class BudgetTracker:
    """
    Live accounting for one run. Every limit is checked before the work
    that would consume it, so a limit is never exceeded and then noticed.
    """

    budget: Budget
    corpus_root: Path
    existing_corpus_bytes: int = 0
    items_this_run: int = 0
    bytes_this_run: int = 0
    stopped_reason: str = ""
    _checks: list[str] = field(default_factory=list)

    def check_can_download(self, estimated_bytes: int = 0) -> None:
        """
        Raise `BudgetExhausted` if this download would breach any limit.

        Free disk is re-read every time rather than cached: another
        process can fill the disk while a harvest runs, and a stale
        reading is exactly as bad as no reading.
        """
        if self.items_this_run >= self.budget.max_items_per_run:
            raise BudgetExhausted(
                "max_items_per_run",
                f"Reached the per-run item limit ({self.budget.max_items_per_run})",
            )

        if self.bytes_this_run >= self.budget.max_download_bytes_per_run:
            raise BudgetExhausted(
                "max_download_bytes_per_run",
                f"Reached the per-run download limit ({human_bytes(self.budget.max_download_bytes_per_run)})",
            )

        projected_run = self.bytes_this_run + max(0, estimated_bytes)
        if estimated_bytes and projected_run > self.budget.max_download_bytes_per_run:
            raise BudgetExhausted(
                "max_download_bytes_per_run",
                f"Next download ({human_bytes(estimated_bytes)}) would exceed the per-run limit",
            )

        if self.budget.max_total_corpus_bytes:
            projected_total = self.existing_corpus_bytes + self.bytes_this_run + max(0, estimated_bytes)
            if projected_total > self.budget.max_total_corpus_bytes:
                raise BudgetExhausted(
                    "max_total_corpus_bytes",
                    f"Corpus would reach {human_bytes(projected_total)}, over the "
                    f"{human_bytes(self.budget.max_total_corpus_bytes)} ceiling",
                )

        if self.budget.min_free_disk_bytes:
            free = free_disk_bytes(self.corpus_root)
            if free - max(0, estimated_bytes) < self.budget.min_free_disk_bytes:
                raise BudgetExhausted(
                    "min_free_disk_bytes",
                    f"Only {human_bytes(free)} free; the configured floor is "
                    f"{human_bytes(self.budget.min_free_disk_bytes)}",
                )

    def record_download(self, size_bytes: int) -> None:
        self.items_this_run += 1
        self.bytes_this_run += max(0, size_bytes)

    def remaining_items(self) -> int:
        return max(0, self.budget.max_items_per_run - self.items_this_run)

    def to_dict(self) -> dict:
        return {
            "items_this_run": self.items_this_run,
            "bytes_this_run": self.bytes_this_run,
            "bytes_this_run_human": human_bytes(self.bytes_this_run),
            "existing_corpus_bytes": self.existing_corpus_bytes,
            "free_disk_bytes": free_disk_bytes(self.corpus_root),
            "stopped_reason": self.stopped_reason,
            "limits": self.budget.to_dict(),
        }


def check_budget(budget: Budget, corpus_root: Path, profile: str, *, autonomous: bool) -> None:
    """
    Verify a run is permitted to start.

    `autonomous` covers `sync`, `resume`, and `daemon-once` -- anything
    that downloads without a human watching. `plan` and `discover` do not
    download and are not gated.

    The error message carries a concrete suggested configuration computed
    from actual free space, because "configure a budget" without numbers
    is an unhelpful refusal.
    """
    if not autonomous:
        return

    problems: list[str] = []
    if budget.max_total_corpus_bytes <= 0:
        problems.append("budget.max_total_corpus_bytes is not set")
    if budget.min_free_disk_bytes <= 0:
        problems.append("budget.min_free_disk_bytes is not set")

    spec = ACQUISITION_PROFILES.get(profile, {})
    if spec.get("requires_explicit_budget") and budget.max_total_corpus_bytes <= 0:
        problems.append(f"the {profile!r} profile requires an explicit storage budget")

    if not problems:
        free = free_disk_bytes(corpus_root)
        if free < budget.min_free_disk_bytes:
            raise BudgetNotConfigured(
                f"Refusing to start: only {human_bytes(free)} free, below the configured "
                f"{human_bytes(budget.min_free_disk_bytes)} floor. Free space or lower the floor."
            )
        return

    suggestion = suggest_budget(corpus_root)
    raise BudgetNotConfigured(
        "Refusing to start an autonomous harvest without a storage budget.\n"
        + "\n".join(f"  - {p}" for p in problems)
        + "\n\nFree space here: "
        + human_bytes(suggestion["free_bytes"])
        + "\nA conservative starting point for config/corpus_policy.yaml:\n\n"
        + "budget:\n"
        + f"  max_total_corpus_bytes: {suggestion['max_total_corpus_bytes']}"
        + f"      # {human_bytes(suggestion['max_total_corpus_bytes'])}\n"
        + f"  min_free_disk_bytes: {suggestion['min_free_disk_bytes']}"
        + f"          # {human_bytes(suggestion['min_free_disk_bytes'])} always kept free\n"
        + f"  max_download_bytes_per_run: {suggestion['max_download_bytes_per_run']}"
        + f"   # {human_bytes(suggestion['max_download_bytes_per_run'])} per run\n\n"
        + "These are a suggestion, not a default: write them into the file deliberately."
    )


class RunLock:
    """
    A PID lockfile under `data/corpus/locks/`.

    Used as a context manager. Acquisition is `O_CREAT | O_EXCL`, which
    is atomic on every POSIX filesystem, so two processes racing cannot
    both win. A lock whose PID no longer exists is reclaimed -- a
    machine that lost power mid-harvest must not need manual cleanup
    before the next run.
    """

    def __init__(self, lock_dir: Path, name: str = "harvest", stale_after_hours: float = 12.0) -> None:
        self.path = lock_dir / f"{name}.lock"
        self.stale_after_hours = stale_after_hours
        self._acquired = False

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat(), "host": os.uname().nodename}
        )

        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            if self._reclaim_if_stale():
                return self.acquire()
            holder = self._read_holder()
            raise LockHeld(
                f"Another harvester run holds {self.path} "
                f"(pid {holder.get('pid', '?')}, started {holder.get('started_at', '?')}). "
                f"If that process is gone, remove the lockfile."
            ) from None
        else:
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
            self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self._acquired = False

    def _read_holder(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _reclaim_if_stale(self) -> bool:
        holder = self._read_holder()
        pid = holder.get("pid")

        if isinstance(pid, int) and _process_alive(pid):
            started = holder.get("started_at", "")
            try:
                age = datetime.now(UTC) - datetime.fromisoformat(started)
            except (TypeError, ValueError):
                return False
            if age > timedelta(hours=self.stale_after_hours):
                logger.warning(
                    "Lock held by live pid %s for %.1f hours (over the %.1f-hour staleness limit); reclaiming",
                    pid, age.total_seconds() / 3600, self.stale_after_hours,
                )
                self.path.unlink(missing_ok=True)
                return True
            return False

        logger.info("Reclaiming stale lockfile %s (pid %s is gone)", self.path, pid)
        self.path.unlink(missing_ok=True)
        return True


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but belongs to another user.
        return True
    return True


def next_run_due(last_finished_at: str | None, interval_hours: int) -> bool:
    """
    Whether a periodic run is due. Used by `daemon-once` so a timer that
    fires more often than the configured interval simply exits, rather
    than harvesting on every tick.
    """
    if not last_finished_at:
        return True
    try:
        last = datetime.fromisoformat(last_finished_at)
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return datetime.now(UTC) - last >= timedelta(hours=interval_hours)


__all__ = [
    "BudgetExhausted",
    "BudgetNotConfigured",
    "BudgetTracker",
    "LockHeld",
    "RunLock",
    "check_budget",
    "next_run_due",
]
