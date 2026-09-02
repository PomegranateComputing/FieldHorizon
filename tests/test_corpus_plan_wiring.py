"""
The acquisition plan, driving the pipeline.

Until this existed, `build_plan()` was a well-tested module nothing
called: the orchestrator decided whether to download by asking
`if candidate.download_url`. That meant a source declaring it does not
host content produced a stream of download failures instead of
`METADATA_ONLY` records — the exact Phase I behaviour the whole graph was
built to remove — and it is why `gallica_text` shipped disabled.

These tests assert the plan actually decides, by running the real
orchestrator against adapters whose declared roles differ.
"""

from __future__ import annotations

import pytest

from fieldhorizon.corpus.adapters.base import (
    ADAPTER_REGISTRY,
    DiscoveryResult,
    SourceAdapter,
)
from fieldhorizon.corpus.capabilities import Capability, PlanStatus, SourceCapabilities
from fieldhorizon.corpus.models import (
    BLOCKED_ACQUISITION_STATES,
    NON_ERROR_ACQUISITION_STATES,
    Candidate,
    Evidence,
    RightsSignal,
    State,
)
from fieldhorizon.corpus.orchestrator import _PLAN_STATUS_TO_STATE, Orchestrator
from fieldhorizon.db import init_db
from tests.corpus_helpers import make_config, make_fetcher, make_policy, make_source

CC0 = "https://creativecommons.org/publicdomain/zero/1.0/"
BOOK = b"A short but complete work. " * 120


def candidate(source_id: str, *, download_url: str = "", **extra) -> Candidate:
    return Candidate(
        source_id=source_id,
        external_id="doc-1",
        title="A Described Work",
        authors=("An Author",),
        language="en",
        canonical_url="https://example.org/doc-1",
        download_url=download_url,
        download_format="txt" if download_url else "",
        rights=RightsSignal(
            content_license=CC0,
            rights_statement_uri=CC0,
            evidence=(Evidence(kind="test:licence", value=CC0, url="https://example.org/doc-1"),),
        ),
        raw_metadata=extra,
    )


def make_adapter(name: str, capabilities: SourceCapabilities, cand: Candidate):
    """Register a throwaway adapter for the duration of one test."""

    class _Adapter(SourceAdapter):
        adapter_name = name

        def discover_page(self, cursor):
            return DiscoveryResult(candidates=[cand], exhausted=True)

        def fetch_content(self, c, max_bytes: int = 0):
            from fieldhorizon.corpus.http import FetchResponse
            from fieldhorizon.corpus.security import sha256_bytes

            return FetchResponse(
                url=c.download_url, final_url=c.download_url, status_code=200,
                content=BOOK, headers={}, detected_mime="text/plain",
                declared_mime="text/plain", encoding="utf-8", sha256=sha256_bytes(BOOK),
            )

    _Adapter.capabilities = capabilities
    _Adapter.__name__ = f"Adapter_{name}"
    ADAPTER_REGISTRY[name] = _Adapter
    return _Adapter


@pytest.fixture
def registered():
    """Undo registry mutations so tests cannot leak adapters into each other."""
    before = dict(ADAPTER_REGISTRY)
    yield
    ADAPTER_REGISTRY.clear()
    ADAPTER_REGISTRY.update(before)


def run(tmp_path, adapter_name: str, source_kwargs: dict | None = None):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    source = make_source(
        adapter_name, adapter_name,
        base_url="https://example.org/feed",
        allowed_hosts=["example.org"],
        **(source_kwargs or {}),
    )
    orchestrator = Orchestrator(
        cfg, make_policy(), [source], fetcher=make_fetcher({}), emit_events=False
    )
    orchestrator.discover()
    orchestrator.acquire(orchestrator.plan())
    return orchestrator


def state_of(orchestrator) -> State:
    items = orchestrator.repo.items_in_states(list(State))
    assert items, "the pipeline produced no item at all"
    return items[0].state


# ------------------------------------------------------------ the map


def test_every_non_actionable_plan_status_has_a_state():
    """
    A table, not a chain of ifs, so that adding a status without deciding
    where its documents rest is a KeyError rather than a silent
    fall-through to "download it anyway".
    """
    for status in PlanStatus:
        if status is PlanStatus.READY_FOR_DOWNLOAD:
            continue
        assert status in _PLAN_STATUS_TO_STATE, status
        assert _PLAN_STATUS_TO_STATE[status] in (
            NON_ERROR_ACQUISITION_STATES | BLOCKED_ACQUISITION_STATES
        )


# ----------------------------------------------- roles drive the outcome


def test_a_source_that_declares_no_content_role_parks_in_metadata_only(tmp_path, registered):
    """
    **The Gallica case.** A provider that discovers, describes, and
    evidences rights but does not host content has done its job
    completely. Before the plan was wired in, this produced a download
    failure.
    """
    make_adapter(
        "describes_only",
        SourceCapabilities(
            provider_id="describes_only",
            capabilities=frozenset({
                Capability.DISCOVERY, Capability.METADATA, Capability.RIGHTS_EVIDENCE,
            }),
        ),
        candidate("describes_only"),
    )

    orchestrator = run(tmp_path, "describes_only")

    assert state_of(orchestrator) == State.METADATA_ONLY
    assert orchestrator.stats.plan_parked == {"METADATA_ONLY": 1}
    assert orchestrator.stats.errors == 0, "a complete metadata acquisition is not an error"
    assert orchestrator.stats.downloaded == 0


def test_a_source_that_hosts_content_still_downloads(tmp_path, registered):
    """The plan must not become a way to never download anything."""
    make_adapter(
        "hosts_content",
        SourceCapabilities(
            provider_id="hosts_content",
            capabilities=frozenset({
                Capability.DISCOVERY, Capability.METADATA,
                Capability.RIGHTS_EVIDENCE, Capability.CONTENT_HOSTING,
            }),
            content_hosts=("example.org",),
        ),
        candidate("hosts_content", download_url="https://example.org/doc-1.txt"),
    )

    orchestrator = run(tmp_path, "hosts_content")

    assert orchestrator.stats.downloaded == 1
    assert orchestrator.stats.plan_parked == {}
    assert state_of(orchestrator) not in (State.METADATA_ONLY, State.PROVIDER_UNVERIFIED)


def test_a_content_url_on_a_non_allowlisted_host_is_refused(tmp_path, registered):
    """
    **The broker case.** An aggregator that can name any host on the
    internet is an allowlist bypass, however open the licence looks.
    """
    make_adapter(
        "broker",
        SourceCapabilities(
            provider_id="broker",
            capabilities=frozenset({
                Capability.DISCOVERY, Capability.METADATA,
                Capability.RIGHTS_EVIDENCE, Capability.CONTENT_HOSTING,
            }),
            content_hosts=("example.org",),
        ),
        candidate("broker", download_url="https://somewhere-else.invalid/doc-1.txt"),
    )

    orchestrator = run(tmp_path, "broker")

    assert state_of(orchestrator) == State.PROVIDER_UNVERIFIED
    assert orchestrator.stats.downloaded == 0
    assert "PROVIDER_UNVERIFIED" in orchestrator.stats.plan_parked


def test_a_credentialled_content_role_parks_in_auth_required(tmp_path, registered, monkeypatch):
    """
    Not "broken" -- a credential that is not configured. The reason names
    the variable so an operator knows what to set.
    """
    monkeypatch.delenv("A_TEST_TOKEN", raising=False)
    make_adapter(
        "needs_token",
        SourceCapabilities(
            provider_id="needs_token",
            capabilities=frozenset({
                Capability.DISCOVERY, Capability.METADATA, Capability.RIGHTS_EVIDENCE,
            }),
            credentialled_capabilities=frozenset({Capability.CONTENT_HOSTING}),
            credential_env="A_TEST_TOKEN",
            content_hosts=("example.org",),
        ),
        candidate("needs_token", download_url="https://example.org/doc-1.txt"),
    )

    orchestrator = run(tmp_path, "needs_token")

    assert state_of(orchestrator) == State.AUTH_REQUIRED
    assert orchestrator.stats.errors == 0
    plan = orchestrator.repo.get_plan(
        orchestrator.repo.items_in_states([State.AUTH_REQUIRED])[0].document_id
    )
    assert "A_TEST_TOKEN" in plan["reason"]


def test_a_bulk_snapshot_parks_rather_than_failing(tmp_path, registered):
    make_adapter(
        "bulk",
        SourceCapabilities(
            provider_id="bulk",
            capabilities=frozenset({
                Capability.DISCOVERY, Capability.METADATA,
                Capability.RIGHTS_EVIDENCE, Capability.BULK_SNAPSHOT,
            }),
        ),
        candidate("bulk", bulk_snapshot_pending=True),
    )

    orchestrator = run(tmp_path, "bulk")

    assert state_of(orchestrator) == State.BULK_SNAPSHOT_PENDING
    assert orchestrator.stats.errors == 0


# ------------------------------------------------------------ the record


def test_the_plan_is_persisted_and_names_who_filled_each_role(tmp_path, registered):
    make_adapter(
        "hosts_content2",
        SourceCapabilities(
            provider_id="hosts_content2",
            capabilities=frozenset({
                Capability.DISCOVERY, Capability.METADATA,
                Capability.RIGHTS_EVIDENCE, Capability.CONTENT_HOSTING,
            }),
            content_hosts=("example.org",),
        ),
        candidate("hosts_content2", download_url="https://example.org/doc-1.txt"),
    )

    orchestrator = run(tmp_path, "hosts_content2")
    document_id = orchestrator.repo.items_in_states(list(State))[0].document_id
    plan = orchestrator.repo.get_plan(document_id)

    assert plan is not None
    assert plan["discovered_by"] == "hosts_content2"
    assert plan["rights_evidence_from"] == "hosts_content2"
    assert plan["content_hosted_by"] == "hosts_content2"
    assert plan["status"] == PlanStatus.READY_FOR_DOWNLOAD.value


def test_re_planning_updates_the_row_rather_than_appending(tmp_path, registered):
    """
    A document re-planned in a later run -- because a credential appeared,
    or a provider started hosting what it only described -- must show its
    CURRENT plan. The state-event log already records the history.
    """
    make_adapter(
        "replan",
        SourceCapabilities(
            provider_id="replan",
            capabilities=frozenset({Capability.DISCOVERY, Capability.METADATA,
                                    Capability.RIGHTS_EVIDENCE}),
        ),
        candidate("replan"),
    )
    orchestrator = run(tmp_path, "replan")
    document_id = orchestrator.repo.items_in_states(list(State))[0].document_id

    orchestrator.repo.record_plan(
        document_id,
        {"document_key": "replan:doc-1", "status": "READY_FOR_DOWNLOAD",
         "content_hosted_by": "someone_else", "reason": ""},
    )

    assert orchestrator.repo.plans_by_status() == {"READY_FOR_DOWNLOAD": 1}
    assert orchestrator.repo.get_plan(document_id)["content_hosted_by"] == "someone_else"


def test_a_parked_document_never_reaches_the_retrieval_index(tmp_path, registered):
    """
    The standing rule, checked at the end of the pipeline this time
    rather than in the state table.
    """
    from fieldhorizon.corpus.models import INDEXABLE_STATES

    make_adapter(
        "parked",
        SourceCapabilities(
            provider_id="parked",
            capabilities=frozenset({Capability.DISCOVERY, Capability.METADATA,
                                    Capability.RIGHTS_EVIDENCE}),
        ),
        candidate("parked"),
    )

    orchestrator = run(tmp_path, "parked")

    assert state_of(orchestrator) not in INDEXABLE_STATES
    assert orchestrator.repo.items_in_states(list(INDEXABLE_STATES)) == []
