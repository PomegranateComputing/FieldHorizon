"""
The pipeline end to end, against a temporary database and local fixtures.

Covers required cases 14 (export guard), 15 (resume after interruption),
16 (idempotence), 36 (materialization), 37 (ingestion into the existing
Field Horizon pipeline), 38 (provenance reaching retrieval), and 39
(de-indexing after a rights change) -- plus the state machine, budget
enforcement, and the concurrency lock.

Everything runs offline: the orchestrator is constructed with a
`FixtureFetcher`, so a full harvest completes without a socket.
"""

from __future__ import annotations

import json

import pytest

from fieldhorizon.corpus.ingestion import (
    MANIFEST_PREFIX,
    corpus_provenance_for_refs,
    indexed_document_ids,
    manifest_id_for,
    withdraw_document,
)
from fieldhorizon.corpus.materialization import AUTO_SUBDIR, destination_dir, materialize
from fieldhorizon.corpus.models import (
    INDEXABLE_STATES,
    TERMINAL_STATES,
    Decision,
    Destination,
    DistributionScope,
    IllegalTransition,
    State,
    allowed_transitions,
    can_transition,
    slugify,
)
from fieldhorizon.corpus.orchestrator import Orchestrator
from fieldhorizon.corpus.policy import Budget
from fieldhorizon.corpus.reporting import (
    ExportBlocked,
    build_attributions,
    build_run_report,
    export_safe,
    write_attributions,
    write_run_report,
)
from fieldhorizon.corpus.repository import CorpusRepository
from fieldhorizon.corpus.scheduler import (
    BudgetExhausted,
    BudgetNotConfigured,
    BudgetTracker,
    LockHeld,
    RunLock,
    check_budget,
    next_run_due,
)
from fieldhorizon.corpus.storage import CorpusStorage, mint_document_id
from fieldhorizon.db import init_db
from tests.corpus_helpers import FIXTURES, make_config, make_fetcher, make_policy, make_source

FEED_URL = "https://standardebooks.org/feeds/opds/all"
SOCIAL_CONTRACT_TXT = (
    "https://standardebooks.org/ebooks/jean-jacques-rousseau/the-social-contract/"
    "downloads/social-contract.txt"
)
FRANKENSTEIN_EPUB = (
    "https://standardebooks.org/ebooks/mary-shelley/frankenstein/downloads/frankenstein.epub"
)
FEED_PAGE_2 = "https://standardebooks.org/feeds/opds/all?page=2"
COMMON_SENSE_TXT = (
    "https://standardebooks.org/ebooks/thomas-paine/common-sense/downloads/common-sense.txt"
)


def _book_text() -> bytes:
    """A long, clean, unmistakably narrative text."""
    from tests.corpus_helpers import fixture_text

    base = fixture_text("sample_book.txt")
    varied = "\n\n".join(
        f"CHAPTER {n}\n\nIn the {n}th season of that survey I recorded {n * 7} nests upon the "
        f"eastern face, a number exceeding by {n * 3} the count of the preceding year, which "
        f"the fishermen attributed to the mildness of the {1840 + n} winter rather than to any "
        f"change in the birds. He said as much to me himself. According to Lindqvist, writing "
        f"{n * 11} years earlier, the figures cannot easily be reconciled with mine."
        for n in range(1, 30)
    )
    return (base + "\n\n" + varied).encode("utf-8")


def _manifesto_text() -> bytes:
    """A short, unmistakably programmatic text."""
    from tests.corpus_helpers import fixture_text

    return fixture_text("sample_manifesto.txt").encode("utf-8")


def _feed_fixtures() -> dict:
    """
    The full offline catalogue: both OPDS pages plus every download they
    advertise. Page 2 carries no rel="next", so discovery terminates
    normally instead of failing on a page the fetcher was never given.
    """
    return {
        FEED_URL: FIXTURES / "opds_standard_ebooks.xml",
        FEED_PAGE_2: FIXTURES / "opds_standard_ebooks_page2.xml",
        SOCIAL_CONTRACT_TXT: _book_text(),
        FRANKENSTEIN_EPUB: _book_text(),
        COMMON_SENSE_TXT: _manifesto_text(),
    }


def _pipeline(tmp_path, *, policy=None, fetcher=None, sources=None, dry_run=False):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    policy = policy or make_policy()
    sources = sources or [make_source("standard_ebooks", "standard_ebooks", base_url=FEED_URL)]
    fetcher = fetcher or make_fetcher(
_feed_fixtures())
    orchestrator = Orchestrator(
        cfg, policy, sources, fetcher=fetcher, dry_run=dry_run, emit_events=False
    )
    return cfg, orchestrator


# ------------------------------------------------------------ state machine


def test_the_state_machine_refuses_a_nonsense_transition():
    assert can_transition(State.DISCOVERED, State.RIGHTS_PENDING)
    assert not can_transition(State.DISCOVERED, State.INDEXED)
    assert not can_transition(State.RIGHTS_REJECTED, State.QUEUED)


def test_terminal_states_have_no_way_out():
    for state in TERMINAL_STATES:
        assert allowed_transitions(state) == frozenset(), state


def test_only_ingested_and_indexed_documents_are_considered_indexable():
    """
    Rule 11: nothing quarantined, stale, or withdrawn may sit in the
    active index.
    """
    assert frozenset({State.INGESTED, State.INDEXED}) == INDEXABLE_STATES
    for excluded in (
        State.RIGHTS_QUARANTINED, State.RIGHTS_REJECTED, State.STALE,
        State.WITHDRAWN, State.QUALITY_REJECTED, State.DUPLICATE,
    ):
        assert excluded not in INDEXABLE_STATES


def test_the_repository_rejects_an_illegal_transition(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)

    from fieldhorizon.corpus.models import Candidate

    candidate = Candidate(source_id="s", external_id="e", title="T")
    document_id = mint_document_id("s", "e")
    repo.create_item(document_id, candidate, "cand", "run")

    with pytest.raises(IllegalTransition):
        repo.transition(document_id, State.INDEXED, "skipping the whole pipeline")


def test_every_transition_writes_an_event(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)

    from fieldhorizon.corpus.models import Candidate

    document_id = mint_document_id("s", "e")
    repo.create_item(document_id, Candidate(source_id="s", external_id="e", title="T"), "c", "run")
    repo.transition(document_id, State.RIGHTS_ACCEPTED, "accepted", "run")
    repo.transition(document_id, State.QUEUED, "selected", "run")

    events = repo.state_events(document_id)
    assert [e["new_state"] for e in events] == ["RIGHTS_ACCEPTED", "QUEUED"]
    assert all(e["run_id"] == "run" and e["pipeline_version"] for e in events)


def test_state_and_columns_change_in_one_transaction(tmp_path):
    """
    A document must never be observable as NORMALIZED without its hash,
    or MATERIALIZED without its path.
    """
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)

    from fieldhorizon.corpus.models import Candidate

    document_id = mint_document_id("s", "e")
    repo.create_item(document_id, Candidate(source_id="s", external_id="e", title="T"), "c", "run")
    repo.transition(document_id, State.RIGHTS_ACCEPTED, "", "run", updates={"normalized_license": "cc0-1.0"})

    item = repo.get_item(document_id)
    assert item.state == State.RIGHTS_ACCEPTED
    assert item.normalized_license == "cc0-1.0"


def test_column_names_in_updates_are_validated(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)

    from fieldhorizon.corpus.models import Candidate

    document_id = mint_document_id("s", "e")
    repo.create_item(document_id, Candidate(source_id="s", external_id="e", title="T"), "c", "run")

    with pytest.raises(ValueError, match="Refusing to build SQL"):
        repo.update_item(document_id, **{"title = 'x'; DROP TABLE corpus_items; --": "x"})


# ---------------------------------------------------------- full harvest


def test_a_full_harvest_discovers_acquires_classifies_and_indexes(tmp_path):
    cfg, orchestrator = _pipeline(tmp_path)
    stats = orchestrator.run_sync("broad")

    assert stats.discovered >= 2
    assert stats.rights_accepted >= 2
    assert stats.downloaded >= 1
    assert stats.normalized >= 1
    assert stats.materialized >= 1
    assert stats.ingested >= 1

    repo = CorpusRepository(cfg.database)
    indexed = repo.items_in_states([State.INDEXED])
    assert indexed, "at least one document should have reached INDEXED"

    for item in indexed:
        decision = repo.latest_rights_decision(item.document_id)
        assert decision["decision"] == "accept"
        assert json.loads(decision["evidence_json"]), "no accept without recorded evidence"


def test_a_borrow_only_candidate_never_reaches_the_index(tmp_path):
    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    repo = CorpusRepository(cfg.database)
    borrowable = repo.find_candidate("standard_ebooks", "https://example.org/ebooks/restricted-title")

    assert borrowable is not None
    assert borrowable["state"] in (State.RIGHTS_REJECTED.value, State.RIGHTS_QUARANTINED.value)
    assert repo.find_item("standard_ebooks", "https://example.org/ebooks/restricted-title") is None


def test_a_dry_run_downloads_nothing(tmp_path):
    cfg, orchestrator = _pipeline(tmp_path, dry_run=True)
    stats = orchestrator.run_sync("broad")

    assert stats.discovered >= 2
    assert stats.downloaded == 0
    assert stats.ingested == 0
    assert orchestrator.fetcher.bytes_downloaded == 0 or not any(
        call.endswith(".txt") for call in orchestrator.fetcher.calls
    )


def test_a_plan_is_always_written_before_the_first_acquisition(tmp_path):
    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    plan_path = CorpusStorage(cfg.root / "data" / "corpus").reports / f"{orchestrator.run_id}_plan.json"
    assert plan_path.exists()

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["run_id"] == orchestrator.run_id
    assert plan["rights_profile"] == "local_research_us"
    assert isinstance(plan["entries"], list)


# ------------------------------------------------------- 16. idempotence


def test_a_second_identical_run_downloads_nothing_new(tmp_path):
    """
    Required case 16. The second run must find everything already known
    and acquire nothing.
    """
    cfg, first = _pipeline(tmp_path)
    first_stats = first.run_sync("broad")
    assert first_stats.downloaded >= 1

    policy = make_policy()
    sources = [make_source("standard_ebooks", "standard_ebooks", base_url=FEED_URL)]
    fetcher = make_fetcher(
_feed_fixtures())
    second = Orchestrator(cfg, policy, sources, fetcher=fetcher, emit_events=False)
    second_stats = second.run_sync("broad")

    assert second_stats.discovered == 0, "re-walking the same catalogue discovers nothing new"
    assert second_stats.already_known >= 2
    assert second_stats.downloaded == 0, "an unchanged document must not be re-downloaded"


def test_re_ingesting_an_unchanged_document_is_a_no_op(tmp_path):
    from fieldhorizon.corpus.ingestion import ingest_document

    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    repo = CorpusRepository(cfg.database)
    item = repo.items_in_states([State.INDEXED])[0]

    outcome = ingest_document(cfg, repo, item)
    assert outcome.status == "unchanged"
    assert outcome.chunk_count == 0


def test_the_chunk_count_does_not_grow_across_runs(tmp_path):
    from fieldhorizon.db import connect

    cfg, first = _pipeline(tmp_path)
    first.run_sync("broad")

    with connect(cfg.database) as conn:
        after_first = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        fts_first = conn.execute("SELECT COUNT(*) AS n FROM chunks_fts").fetchone()["n"]

    fetcher = make_fetcher(
_feed_fixtures())
    second = Orchestrator(
        cfg, make_policy(), [make_source("standard_ebooks", "standard_ebooks", base_url=FEED_URL)],
        fetcher=fetcher, emit_events=False,
    )
    second.run_sync("broad")

    with connect(cfg.database) as conn:
        after_second = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        fts_second = conn.execute("SELECT COUNT(*) AS n FROM chunks_fts").fetchone()["n"]

    assert after_second == after_first
    # chunks_fts has no foreign key to chunks; without explicit cleanup it
    # accumulates ghost rows on every re-ingest.
    assert fts_second == fts_first


# ------------------------------------------------------------- 15. resume


def test_a_run_interrupted_mid_download_resumes(tmp_path):
    """
    Required case 15. The first attempt fails at the download step,
    leaving the document resumable; a later run with a working fetcher
    picks it up from exactly there.
    """
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    policy = make_policy()
    sources = [make_source("standard_ebooks", "standard_ebooks", base_url=FEED_URL)]

    # A network interruption partway through: the catalogue was read,
    # then the connection dropped. Deliberately a RETRYABLE failure --
    # a 404 would be final, and correctly so.
    broken = make_fetcher(_feed_fixtures())
    broken.transient_failures = {SOCIAL_CONTRACT_TXT, FRANKENSTEIN_EPUB, COMMON_SENSE_TXT}
    first = Orchestrator(cfg, policy, sources, fetcher=broken, emit_events=False)
    first.run_sync("broad")

    repo = CorpusRepository(cfg.database)
    stuck = repo.items_in_states([State.FAILED_RETRYABLE, State.QUEUED, State.DOWNLOADING])
    assert stuck, "an interrupted download must leave the document in a resumable state"
    assert not repo.items_in_states([State.INDEXED])

    working = make_fetcher(
_feed_fixtures())
    second = Orchestrator(cfg, policy, sources, fetcher=working, emit_events=False)
    second.resume()

    assert repo.items_in_states([State.INDEXED]), "resume should have completed the interrupted work"


def test_a_failure_is_recorded_with_its_stage_and_retryability(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    # Catalogue reachable, content not.
    broken = make_fetcher({
        FEED_URL: FIXTURES / "opds_standard_ebooks.xml",
        FEED_PAGE_2: FIXTURES / "opds_standard_ebooks_page2.xml",
    })
    orchestrator = Orchestrator(
        cfg, make_policy(), [make_source("standard_ebooks", "standard_ebooks", base_url=FEED_URL)],
        fetcher=broken, emit_events=False,
    )
    orchestrator.run_sync("broad")

    errors = orchestrator.repo.errors_for_run(orchestrator.run_id)
    assert errors
    assert all(e["stage"] for e in errors)


def test_a_source_that_fails_does_not_stop_the_others(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)

    working = make_source("standard_ebooks", "standard_ebooks", base_url=FEED_URL)
    dead = make_source(
        "doab", "oai_pmh",
        base_url="https://directory.doabooks.org/oai/request",
        allowed_hosts=["directory.doabooks.org"],
    )
    fetcher = make_fetcher(
_feed_fixtures())
    orchestrator = Orchestrator(cfg, make_policy(), [working, dead], fetcher=fetcher, emit_events=False)
    stats = orchestrator.run_sync("broad")

    assert stats.discovered >= 2, "the healthy source still produced candidates"
    per_source = stats.per_source
    assert per_source["doab"]["status"] == "failed"
    assert per_source["standard_ebooks"]["status"] == "completed"


# ------------------------------------------------------- 36. materialization


def test_documents_land_in_reserved_auto_directories(tmp_path):
    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    books_dir = destination_dir(cfg, Destination.BOOKS)
    manifesto_dir = destination_dir(cfg, Destination.MANIFESTO)

    assert books_dir.name == AUTO_SUBDIR
    assert books_dir.parent == cfg.books
    # The repository's directory is `manifestos` (plural) while the
    # source_type is the singular `manifesto`; both are preserved.
    assert manifesto_dir.parent == cfg.manifestos
    assert manifesto_dir.parent.name == "manifestos"

    written = list(books_dir.glob("*.txt")) + list(manifesto_dir.glob("*.txt"))
    assert written, "a harvest should have materialized at least one file"
    for path in written:
        assert path.read_text(encoding="utf-8").strip()


def test_a_materialized_filename_carries_its_document_id(tmp_path):
    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    repo = CorpusRepository(cfg.database)
    item = repo.items_in_states([State.INDEXED])[0]
    assert item.document_id in item.materialized_path


def test_the_metadata_sidecar_lives_outside_the_ingested_tree(tmp_path):
    """
    ingest_books globs *.txt recursively. A sidecar placed beside the
    text would be ingested AS CORPUS CONTENT -- indexing a document's own
    licence metadata as though it were a philosophical work.
    """
    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    repo = CorpusRepository(cfg.database)
    item = repo.items_in_states([State.INDEXED])[0]
    sidecar = CorpusStorage(cfg.root / "data" / "corpus").metadata_path(item.document_id)

    assert sidecar.exists()
    assert cfg.books not in sidecar.parents
    assert cfg.manifestos not in sidecar.parents

    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["untrusted_content"] is True
    assert payload["rights"]["decision"]["decision"] == "accept"
    assert payload["provenance"]["normalized_sha256"]


def test_materialization_refuses_a_document_without_an_accepted_decision(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)
    storage = CorpusStorage(cfg.root / "data" / "corpus")
    storage.ensure_layout()

    from fieldhorizon.corpus.models import Candidate

    document_id = mint_document_id("s", "e")
    repo.create_item(document_id, Candidate(source_id="s", external_id="e", title="T"), "c", "run")
    digest, _, _ = storage.store_normalized("some text")
    repo.update_item(document_id, normalized_sha256=digest, destination="books")

    item = repo.get_item(document_id)
    with pytest.raises(ValueError, match="accepted rights decision"):
        materialize(cfg, repo, storage, item)


def test_the_raw_download_is_kept_immutable_alongside_the_normalized_text(tmp_path):
    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    repo = CorpusRepository(cfg.database)
    item = repo.items_in_states([State.INDEXED])[0]
    kinds = {a["kind"] for a in repo.artifacts(item.document_id)}

    assert "raw" in kinds
    assert "normalized" in kinds

    storage = CorpusStorage(cfg.root / "data" / "corpus")
    assert storage.blob_path(item.raw_sha256).exists()
    assert storage.normalized_path(item.normalized_sha256).exists()


# ------------------------------------------------- 37/38. ingestion + retrieval


def test_harvested_documents_are_ingested_as_ordinary_field_horizon_sources(tmp_path):
    from fieldhorizon.db import connect

    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    with connect(cfg.database) as conn:
        rows = conn.execute(
            "SELECT title, source_type, license, weight, manifest_id, language FROM sources "
            "WHERE manifest_id LIKE ?", (f"{MANIFEST_PREFIX}%",)
        ).fetchall()

    assert rows
    for row in rows:
        # The source_type values retrieval routing already knows -- not a
        # new one that would make harvested documents invisible.
        assert row["source_type"] in ("book", "manifesto")
        assert row["license"]
        assert row["manifest_id"].startswith(MANIFEST_PREFIX)
        # Capped below 1.0: a hand-curated source defaults to 1.0 and an
        # automatically acquired text should not outrank it by default.
        assert 0.0 < row["weight"] < 1.0


def test_chunks_carry_provenance_and_the_untrusted_marker(tmp_path):
    from fieldhorizon.db import connect

    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    with connect(cfg.database) as conn:
        row = conn.execute(
            "SELECT c.metadata FROM chunks c JOIN sources s ON s.id = c.source_id "
            "WHERE s.manifest_id LIKE ? LIMIT 1", (f"{MANIFEST_PREFIX}%",)
        ).fetchone()

    metadata = json.loads(row["metadata"])
    assert metadata["untrusted_content"] is True
    assert metadata["corpus_document_id"]
    assert metadata["license"]
    assert metadata["canonical_url"]
    assert metadata["normalized_sha256"]


def test_provenance_reaches_the_retrieval_layer(tmp_path):
    """
    Required case 38. A retrieved chunk must arrive carrying its
    document id, title, source, licence, canonical URL, and hash -- and
    marked as untrusted third-party data.
    """
    from fieldhorizon.db import connect
    from fieldhorizon.retrieval import UNTRUSTED_CONTENT_NOTICE, attach_corpus_provenance

    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    with connect(cfg.database) as conn:
        rows = conn.execute(
            "SELECT c.canonical_ref, c.content, s.title AS source_title, s.source_type "
            "FROM chunks c JOIN sources s ON s.id = c.source_id "
            "WHERE s.manifest_id LIKE ? LIMIT 3", (f"{MANIFEST_PREFIX}%",)
        ).fetchall()
    assert rows

    enriched = attach_corpus_provenance(cfg, rows)
    assert enriched

    first = enriched[0]
    assert first["untrusted_content"] is True
    assert first["untrusted_content_notice"] == UNTRUSTED_CONTENT_NOTICE

    provenance = first["provenance"]
    for field in (
        "document_id", "title", "destination", "source", "canonical_url",
        "license", "provenance_sha256",
    ):
        assert provenance[field], f"{field} missing from retrieval provenance"
    assert provenance["destination"] in ("books", "manifesto")


def test_hand_curated_chunks_pass_through_retrieval_unchanged(tmp_path):
    """
    Historical documents have no sidecar and must keep working exactly as
    before: a missing provenance key means "not harvested", never an error.
    """
    from fieldhorizon.retrieval import attach_corpus_provenance

    cfg = make_config(tmp_path)
    init_db(cfg.database)

    rows = [{"canonical_ref": "Some Hand Curated Book / chunk 00000", "content": "text"}]
    enriched = attach_corpus_provenance(cfg, rows)

    assert len(enriched) == 1
    assert "provenance" not in enriched[0]
    assert "untrusted_content" not in enriched[0]
    assert enriched[0]["content"] == "text"


def test_provenance_lookup_returns_only_harvested_chunks(tmp_path):
    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    found = corpus_provenance_for_refs(cfg, ["definitely not a real canonical ref"])
    assert found == {}


# -------------------------------------------- 39. de-indexing on rights change


def test_a_rights_audit_removes_stale_documents_from_the_index(tmp_path):
    """
    Required case 39. When licence evidence goes stale the document
    leaves the active index immediately -- while its file, blob, sidecar,
    and every prior decision row are preserved.
    """
    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    repo = CorpusRepository(cfg.database)
    before = indexed_document_ids(cfg)
    assert before

    item = repo.items_in_states([State.INDEXED])[0]
    storage = CorpusStorage(cfg.root / "data" / "corpus")
    blob = storage.normalized_path(item.normalized_sha256)
    sidecar = storage.metadata_path(item.document_id)
    decisions_before = len(repo.rights_decisions(item.document_id))

    # stale_after_days=0 makes every document's evidence stale.
    results = orchestrator.audit_rights(stale_after_days=0)
    assert results["stale"] >= 1

    after = indexed_document_ids(cfg)
    assert item.document_id not in after

    refreshed = repo.get_item(item.document_id)
    assert refreshed.state == State.STALE

    # Nothing was destroyed.
    assert blob.exists()
    assert sidecar.exists()
    assert len(repo.rights_decisions(item.document_id)) == decisions_before


def test_withdrawing_a_document_removes_its_chunks_and_fts_rows(tmp_path):
    from fieldhorizon.db import connect

    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    repo = CorpusRepository(cfg.database)
    item = repo.items_in_states([State.INDEXED])[0]

    with connect(cfg.database) as conn:
        source_id = conn.execute(
            "SELECT id FROM sources WHERE manifest_id = ?", (manifest_id_for(item.document_id),)
        ).fetchone()["id"]
        chunks_before = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_id = ?", (source_id,)
        ).fetchone()["n"]
    assert chunks_before > 0

    assert withdraw_document(cfg, item.document_id) is True

    with connect(cfg.database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_id = ?", (source_id,)
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE id = ?", (source_id,)
        ).fetchone()["n"] == 0
        # The FTS index has no foreign key; ghost rows would survive
        # without an explicit delete.
        orphans = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks_fts WHERE canonical_ref NOT IN "
            "(SELECT canonical_ref FROM chunks)"
        ).fetchone()["n"]
        assert orphans == 0


def test_withdrawing_an_unknown_document_is_a_harmless_no_op(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    assert withdraw_document(cfg, "no_such_document") is False


# ------------------------------------------------------- 14. export guard


def _accepted_item(repo, cfg, *, document_id, scope, licence, destination="books"):
    """Insert a fully-accepted item directly, for export-guard tests."""
    from fieldhorizon.corpus.models import Candidate, Evidence, RightsDecision

    candidate = Candidate(source_id="src", external_id=document_id, title=f"Title {document_id}")
    repo.create_item(document_id, candidate, "cand", "run")

    storage = CorpusStorage(cfg.root / "data" / "corpus")
    storage.ensure_layout()
    digest, path, _ = storage.store_normalized(f"The text of {document_id}. " * 50)

    repo.record_rights_decision(
        RightsDecision(
            decision=Decision.ACCEPT,
            normalized_license=licence,
            rights_scope=scope,
            commercial_use=True,
            redistribution=True,
            derivatives=True,
            attribution_required=False,
            share_alike=False,
            evidence=(Evidence(kind="test", value="recorded", url="https://example.org"),),
            policy_profile="local_research_us",
        ),
        document_id=document_id,
        run_id="run",
    )
    repo.update_item(
        document_id,
        normalized_sha256=digest,
        destination=destination,
        normalized_license=licence,
        distribution_scope=scope.value,
        materialized_path=str(path),
        state=State.INGESTED.value,
    )
    return document_id


def test_export_refuses_us_only_material_under_release_worldwide(tmp_path):
    """
    Required case 14. This is the last line of defence for the rule that
    a Gutenberg text is usable locally and is NOT worldwide public domain.
    """
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)

    _accepted_item(repo, cfg, document_id="worldwide_doc",
                   scope=DistributionScope.WORLDWIDE, licence="cc0-1.0")
    _accepted_item(repo, cfg, document_id="us_only_doc",
                   scope=DistributionScope.LOCAL_US_ONLY, licence="public-domain-us")

    with pytest.raises(ExportBlocked) as excinfo:
        export_safe(cfg, repo, "release_worldwide", tmp_path / "release")

    message = str(excinfo.value)
    assert "us_only_doc" in message
    assert "--filter" in message
    assert not (tmp_path / "release").exists()


def test_export_with_filter_produces_the_publishable_subset(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)

    _accepted_item(repo, cfg, document_id="worldwide_doc",
                   scope=DistributionScope.WORLDWIDE, licence="cc0-1.0")
    _accepted_item(repo, cfg, document_id="us_only_doc",
                   scope=DistributionScope.LOCAL_US_ONLY, licence="public-domain-us")

    result = export_safe(cfg, repo, "release_worldwide", tmp_path / "release", filter_incompatible=True)

    assert result.included == 1
    assert result.excluded == 1
    assert result.excluded_documents[0]["document_id"] == "us_only_doc"

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["rights_profile"] == "release_worldwide"
    assert manifest["included_count"] == 1
    assert (tmp_path / "release" / "ATTRIBUTIONS.md").exists()

    exported = list((tmp_path / "release" / "texts").rglob("*.txt"))
    assert len(exported) == 1


def test_the_local_profile_exports_both_scopes(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)

    _accepted_item(repo, cfg, document_id="worldwide_doc",
                   scope=DistributionScope.WORLDWIDE, licence="cc0-1.0")
    _accepted_item(repo, cfg, document_id="us_only_doc",
                   scope=DistributionScope.LOCAL_US_ONLY, licence="public-domain-us")

    result = export_safe(cfg, repo, "local_research_us", tmp_path / "local")
    assert result.included == 2
    assert result.excluded == 0


def test_attributions_record_obligations_and_scope(tmp_path):
    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    repo = CorpusRepository(cfg.database)
    records = build_attributions(repo)
    assert records

    for record in records:
        assert record["title"]
        assert record["license"]
        assert record["canonical_url"]
        assert record["distribution_scope"] in {s.value for s in DistributionScope}
        assert "attribution_required" in record
        assert "share_alike" in record

    jsonl_path, md_path = write_attributions(cfg, repo)
    assert jsonl_path.exists() and md_path.exists()
    text = md_path.read_text(encoding="utf-8")
    assert "LOCAL_US_ONLY" in text  # the warning about worldwide exports


# ------------------------------------------------------------- budgets


def test_a_run_stops_cleanly_at_its_item_limit(tmp_path):
    policy = make_policy(max_items_per_run=1)
    cfg, orchestrator = _pipeline(tmp_path, policy=policy)
    stats = orchestrator.run_sync("broad")

    assert stats.downloaded <= 1
    if stats.stopped_reason:
        assert "max_items_per_run" in stats.stopped_reason


def test_the_budget_tracker_refuses_before_the_work_not_after(tmp_path):
    tracker = BudgetTracker(
        budget=Budget(max_items_per_run=2, max_download_bytes_per_run=1000,
                      max_total_corpus_bytes=10_000, min_free_disk_bytes=1),
        corpus_root=tmp_path,
    )
    tracker.check_can_download(500)
    tracker.record_download(500)
    tracker.check_can_download(400)
    tracker.record_download(400)

    with pytest.raises(BudgetExhausted) as excinfo:
        tracker.check_can_download(100)
    assert excinfo.value.limit_name == "max_items_per_run"


def test_a_download_that_would_breach_the_byte_ceiling_is_refused(tmp_path):
    tracker = BudgetTracker(
        budget=Budget(max_items_per_run=100, max_download_bytes_per_run=1000,
                      max_total_corpus_bytes=10_000, min_free_disk_bytes=1),
        corpus_root=tmp_path,
    )
    with pytest.raises(BudgetExhausted) as excinfo:
        tracker.check_can_download(2000)
    assert excinfo.value.limit_name == "max_download_bytes_per_run"


def test_autonomous_mode_refuses_to_start_without_a_storage_budget(tmp_path):
    """
    A harvester with no storage ceiling eventually fills the disk. This
    is a refusal, not a warning.
    """
    with pytest.raises(BudgetNotConfigured) as excinfo:
        check_budget(
            Budget(max_total_corpus_bytes=0, min_free_disk_bytes=0),
            tmp_path, "broad", autonomous=True,
        )

    message = str(excinfo.value)
    assert "max_total_corpus_bytes" in message
    # The refusal carries concrete numbers computed from real free space.
    assert "budget:" in message
    assert "suggestion, not a default" in message


def test_plan_and_discover_are_not_budget_gated(tmp_path):
    check_budget(Budget(max_total_corpus_bytes=0, min_free_disk_bytes=0),
                 tmp_path, "broad", autonomous=False)


def test_the_archive_profile_demands_an_explicit_budget(tmp_path):
    with pytest.raises(BudgetNotConfigured, match="archive"):
        check_budget(
            Budget(max_total_corpus_bytes=0, min_free_disk_bytes=1),
            tmp_path, "archive", autonomous=True,
        )


# ---------------------------------------------------------------- locking


def test_two_concurrent_harvests_cannot_run_at_once(tmp_path):
    lock_dir = tmp_path / "locks"
    with RunLock(lock_dir), pytest.raises(LockHeld, match="Another harvester run"):
        RunLock(lock_dir).acquire()


def test_a_lock_is_released_on_exit(tmp_path):
    lock_dir = tmp_path / "locks"
    with RunLock(lock_dir):
        pass
    with RunLock(lock_dir):
        pass  # acquiring again must succeed


def test_a_stale_lock_from_a_dead_process_is_reclaimed(tmp_path):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True)
    # PID 999999 will not exist; a power failure must not require manual
    # cleanup before the next run.
    (lock_dir / "harvest.lock").write_text(
        json.dumps({"pid": 999999, "started_at": "2020-01-01T00:00:00+00:00", "host": "old"}),
        encoding="utf-8",
    )
    with RunLock(lock_dir):
        pass


def test_the_sync_interval_is_honoured():
    assert next_run_due(None, 24) is True
    assert next_run_due("2020-01-01T00:00:00+00:00", 24) is True
    from datetime import UTC, datetime

    assert next_run_due(datetime.now(UTC).isoformat(), 24) is False


# -------------------------------------------------------------- reporting


def test_a_run_report_is_written_in_both_formats(tmp_path):
    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    json_path, md_path = write_run_report(cfg, orchestrator.repo, orchestrator.run_id)
    assert json_path.exists() and md_path.exists()

    report = json.loads(json_path.read_text(encoding="utf-8"))
    for key in (
        "run_id", "profile", "rights_profile", "stats", "sources", "errors",
        "distributions", "coverage", "duplicate_clusters", "disk", "ingested_documents",
    ):
        assert key in report, f"{key} missing from the run report"

    markdown = md_path.read_text(encoding="utf-8")
    assert orchestrator.run_id in markdown
    assert "Counters" in markdown


def test_the_report_never_contains_document_text(tmp_path):
    """
    A harvester log that embedded content would be enormous and would put
    untrusted text somewhere that later gets grepped and pasted around.
    """
    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    report = build_run_report(cfg, orchestrator.repo, orchestrator.run_id)
    serialized = json.dumps(report)

    assert "In the year 1847 I first travelled" not in serialized
    assert "eastern face" not in serialized


def test_the_run_report_distributions_cover_the_required_dimensions(tmp_path):
    cfg, orchestrator = _pipeline(tmp_path)
    orchestrator.run_sync("broad")

    report = build_run_report(cfg, orchestrator.repo, orchestrator.run_id)
    assert set(report["distributions"]) >= {
        "language", "destination", "license", "source", "state", "distribution_scope"
    }
    assert "priority_language_coverage" in report["coverage"]


def test_distribution_queries_reject_an_unknown_column(tmp_path):
    cfg = make_config(tmp_path)
    init_db(cfg.database)
    repo = CorpusRepository(cfg.database)

    with pytest.raises(ValueError, match="Unsupported distribution column"):
        repo.distribution("title; DROP TABLE corpus_items")


# ------------------------------------------------------------------ misc


def test_slugify_neutralizes_untrusted_titles():
    assert slugify("The Social Contract") == "the-social-contract"
    assert "/" not in slugify("../../etc/passwd")
    assert slugify("Déclaration des droits") == "declaration-des-droits"
    assert slugify("") == "untitled"


def test_document_ids_are_stable_and_content_independent():
    """
    Re-downloading a document whose file changed upstream must still
    address the same document, or every upstream correction would fork a
    new document and orphan the old one's provenance.
    """
    first = mint_document_id("gutenberg", "61")
    second = mint_document_id("gutenberg", "61")
    assert first == second
    assert first != mint_document_id("gutenberg", "62")
    assert first.startswith("gutenberg_")


def test_blobs_are_content_addressed_and_never_rewritten(tmp_path):
    storage = CorpusStorage(tmp_path / "corpus")
    storage.ensure_layout()

    digest_a, path_a, new_a = storage.store_blob(b"identical content")
    digest_b, path_b, new_b = storage.store_blob(b"identical content")

    assert digest_a == digest_b
    assert path_a == path_b
    assert new_a is True
    assert new_b is False, "an existing blob must not be rewritten"


def test_storage_refuses_a_non_hex_digest_as_a_path_component(tmp_path):
    storage = CorpusStorage(tmp_path / "corpus")
    with pytest.raises(ValueError, match="non-hex digest"):
        storage.blob_path("../../etc/passwd")
