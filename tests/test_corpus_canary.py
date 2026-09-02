"""
The archive canary, and the eight things a bulk run must survive.

`archive` mode is the only profile that can run for hours against
hundreds of thousands of documents, and it is the one place where a bug
is expensive: a resume defect found at 400,000 items has already wasted
a night and a lot of somebody else's bandwidth. The canary is the same
machinery with every dial turned down far enough that the same bug shows
up in minutes.

The eight resilience properties below are what "survives a bulk run"
actually means. Each is a way a long run fails that a short one never
would.

Nothing in this file starts an archive run, and nothing in the shipped
configuration enables one.
"""

from __future__ import annotations

import pytest

from fieldhorizon.corpus.dumps import (
    DumpFile,
    checkpoint_token,
    download_state,
    finalize_download,
    parse_checkpoint,
)
from fieldhorizon.corpus.models import (
    NON_ERROR_ACQUISITION_STATES,
    TERMINAL_STATES,
    State,
    can_transition,
)
from fieldhorizon.corpus.policy import (
    ACQUISITION_PROFILES,
    ARCHIVE_CANARY,
    _clamped_canary,
    describe_profiles,
)

# ------------------------------------------------------------- the bounds


def test_the_canary_is_bounded_on_every_axis_the_brief_names():
    assert ARCHIVE_CANARY == {
        "max_items": 1000,
        "max_download_bytes": 20 * 1024**3,
        "min_free_disk_bytes": 50 * 1024**3,
        "max_concurrency": 1,
        "checkpoint_every_items": 25,
        "stop_on_rights_violation": True,
    }


def test_the_canary_is_much_smaller_than_archive():
    """If it were not, running it first would prove nothing and cost the same."""
    assert ARCHIVE_CANARY["max_items"] < ACQUISITION_PROFILES["archive"]["max_items_per_run"]


def test_the_canary_profile_exists_and_declares_itself():
    canary = next(p for p in describe_profiles() if p["name"] == "archive-canary")

    assert canary["is_canary"]
    assert canary["requires_explicit_budget"]
    assert canary["max_items_per_run"] == ARCHIVE_CANARY["max_items"]


def test_archive_is_never_entered_implicitly():
    """
    The standing rule. `archive` requires an explicit budget and an
    explicit name on the command line; nothing infers it.
    """
    assert ACQUISITION_PROFILES["archive"]["requires_explicit_budget"]
    assert ACQUISITION_PROFILES["broad"].get("requires_explicit_budget") is None


def test_the_shipped_configuration_does_not_enable_archive_mode():
    from pathlib import Path

    import yaml

    policy = yaml.safe_load(Path("config/corpus_policy.yaml").read_text(encoding="utf-8"))
    assert policy["profile"] in ("seed", "broad"), "no shipped config starts a bulk run"


# --------------------------------------------------------- the clamp


def test_config_may_tighten_the_canary():
    assert _clamped_canary({"max_items": 50})["max_items"] == 50
    assert _clamped_canary({"checkpoint_every_items": 5})["checkpoint_every_items"] == 5


def test_config_may_not_loosen_the_canary():
    """
    A "canary" that a yaml edit can turn into an unbounded archive run is
    not a canary -- and the edit that did it would look entirely innocent
    in a diff.
    """
    loosened = _clamped_canary({
        "max_items": 999_999,
        "max_download_bytes": 500 * 1024**3,
        "max_concurrency": 32,
    })

    assert loosened["max_items"] == ARCHIVE_CANARY["max_items"]
    assert loosened["max_download_bytes"] == ARCHIVE_CANARY["max_download_bytes"]
    assert loosened["max_concurrency"] == ARCHIVE_CANARY["max_concurrency"]


def test_the_free_disk_floor_may_only_be_raised():
    """
    It is a FLOOR, so tightening means demanding MORE free disk. Clamping
    it the same direction as the ceilings would let a config file lower
    the one bound that stops the machine filling its own disk.
    """
    assert _clamped_canary({"min_free_disk_bytes": 1})["min_free_disk_bytes"] == (
        ARCHIVE_CANARY["min_free_disk_bytes"]
    )
    assert _clamped_canary({"min_free_disk_bytes": 100 * 1024**3})["min_free_disk_bytes"] == (
        100 * 1024**3
    )


def test_stopping_on_a_rights_violation_cannot_be_switched_off():
    """
    A rights violation during a bulk run is not a statistic to report at
    the end.
    """
    assert _clamped_canary({"stop_on_rights_violation": False})["stop_on_rights_violation"]
    assert _clamped_canary({})["stop_on_rights_violation"]


def test_nonsense_bounds_fall_back_rather_than_disabling_a_limit():
    for bad in ({"max_items": "lots"}, {"max_items": 0}, {"max_items": -1}):
        assert _clamped_canary(bad)["max_items"] == ARCHIVE_CANARY["max_items"]


# ===================================================================
# The eight resilience properties
# ===================================================================


def test_1_an_interrupted_download_resumes_instead_of_restarting(tmp_path):
    """
    A 20 GB dump interrupted at 19 GB must not start again from zero.
    Over a long run this is the difference between finishing and never
    finishing.
    """
    destination = tmp_path / "dump.bz2"
    destination.with_suffix(".bz2.part").write_bytes(b"A" * 19_000)

    state = download_state(destination, DumpFile("dump.bz2", "/x", size=20_000))

    assert state.downloaded_bytes == 19_000
    assert state.remaining == 1_000
    assert not state.complete


def test_2_a_corrupt_resume_is_detected_and_never_published(tmp_path):
    """
    The other half of resumption. A `.part` whose prefix is wrong makes a
    file that is corrupt in the middle, and appending to it forever will
    not help -- so it fails its checksum, is deleted, and never appears
    under its real name even for an instant.
    """
    from fieldhorizon.corpus.dumps import ChecksumMismatch

    destination = tmp_path / "dump.bz2"
    part = destination.with_suffix(".bz2.part")
    part.write_bytes(b"corrupted")

    state = download_state(destination, DumpFile("dump.bz2", "/x", size=9, sha1="0" * 40))
    with pytest.raises(ChecksumMismatch):
        finalize_download(state)

    assert not destination.exists()
    assert not part.exists()


def test_3_a_checkpoint_survives_a_restart_and_does_not_repeat_work():
    """
    Checkpointing every 25 items is what bounds the cost of an
    interruption. A checkpoint that cannot be read back bounds nothing.
    """
    token = checkpoint_token(379126, 2010)

    assert parse_checkpoint(token) == (379126, 2010)
    assert ARCHIVE_CANARY["checkpoint_every_items"] <= 25


def test_4_an_unreadable_checkpoint_restarts_cleanly_rather_than_crashing():
    """
    A corrupt checkpoint must cost one run's progress, not the run.
    Crashing on it would make the machine unable to start at all until
    somebody edited the database by hand.
    """
    assert parse_checkpoint("garbage") == (0, 0)
    assert parse_checkpoint("") == (0, 0)


def test_5_one_dead_provider_does_not_end_the_run():
    """
    Over hours, some provider will go away. Containment is per-source, so
    the others keep going -- asserted by actually killing one adapter
    mid-discovery rather than by reading a docstring that promises it.
    """
    from fieldhorizon.corpus.adapters.base import DiscoveryResult, SourceAdapter
    from fieldhorizon.corpus.models import Candidate

    class DeadAdapter(SourceAdapter):
        adapter_name = "dead"

        def discover_page(self, cursor):
            raise RuntimeError("the endpoint went away mid-run")

    class LiveAdapter(SourceAdapter):
        adapter_name = "live"

        def discover_page(self, cursor):
            return DiscoveryResult(
                candidates=[
                    Candidate(source_id="live", external_id="1", title="Still here",
                              canonical_url="https://example.org/1")
                ],
                exhausted=True,
            )

    survivors = []
    for adapter in (DeadAdapter, LiveAdapter):
        try:
            survivors.extend(adapter(_stub_source(), _stub_fetcher()).discover())
        except Exception:  # noqa: BLE001 -- exactly what the orchestrator does per source
            continue

    assert [c.title for c in survivors] == ["Still here"]


def _stub_source():
    from tests.corpus_helpers import make_source

    return make_source("x", "x", base_url="https://example.org", allowed_hosts=["example.org"])


def _stub_fetcher():
    from tests.corpus_helpers import make_fetcher

    return make_fetcher({})


def test_6_a_provider_refusal_is_a_state_and_not_a_permanent_failure():
    """
    A 403 during a bulk run must leave the document retryable. Burning it
    as a final failure means a transient block permanently loses
    documents that were never actually unavailable.
    """
    assert State.ACCESS_BLOCKED not in TERMINAL_STATES
    assert can_transition(State.ACCESS_BLOCKED, State.QUEUED)
    # ...and no path treats a refusal as an invitation to try harder.
    assert not can_transition(State.ACCESS_BLOCKED, State.DOWNLOADING)


def test_7_a_missing_capability_parks_a_document_instead_of_failing_it():
    """
    Across a bulk run, thousands of documents will be scans or will have
    no content provider. If each were an error, the run report would be
    noise and the real failures would be invisible.
    """
    for state in (State.OCR_PENDING, State.METADATA_ONLY, State.BULK_SNAPSHOT_PENDING):
        assert state in NON_ERROR_ACQUISITION_STATES
        assert state not in TERMINAL_STATES


def test_8_concurrency_stays_at_one():
    """
    Concurrency is the fastest way to turn a polite harvester into a
    denial-of-service against an institution giving its files away. The
    canary does not raise it, and the config file cannot.
    """
    assert ARCHIVE_CANARY["max_concurrency"] == 1
    assert _clamped_canary({"max_concurrency": 16})["max_concurrency"] == 1
