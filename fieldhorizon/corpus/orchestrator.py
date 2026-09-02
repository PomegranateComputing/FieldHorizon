"""
The pipeline driver.

    DISCOVERY -> RIGHTS -> ELIGIBILITY -> SELECTION -> DOWNLOAD
    -> RAW STORAGE -> NORMALIZATION -> QUALITY -> DEDUPLICATION
    -> CLASSIFICATION -> MATERIALIZATION -> INGESTION -> INDEXING -> AUDIT

Every stage is written so it can be entered from a cold start with only
the database as context. That is what "resumable" means here: `resume`
does not replay a journal, it simply looks at what state each document
is in and runs the stage that state implies. A machine that lost power
mid-download restarts with that document in DOWNLOADING and re-downloads
exactly it.

Failure containment is per-source and per-document. A dead endpoint
fails its own source and nothing else; a malformed file fails its own
document. The run continues and reports both.

Nothing here decides rights. The orchestrator asks `rights.evaluate` and
obeys it, and there is no path from a non-accept decision to
materialization or ingestion -- the gate is checked in the orchestrator,
again in `materialize`, and again in `ingest_document`.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import AppConfig
from ..events import ACTOR_CLI, AGGREGATE_INGEST, OperationEmitter
from . import classification as classification_mod
from . import deduplication as dedup
from . import quality as quality_mod
from .adapters.base import ADAPTER_REGISTRY, SourceConfig
from .adapters.registry import build_adapter
from .capabilities import AcquisitionPlan, PlanStatus, ProviderRegistry, build_plan
from .http import BaseFetcher, BoundedFetcher, FetchError, NotModified
from .ingestion import ingest_document, withdraw_document
from .materialization import dematerialize, materialize
from .models import (
    NORMALIZER_VERSION,
    Candidate,
    Decision,
    Destination,
    DistributionScope,
    ReasonCode,
    State,
)
from .normalization import NormalizationError, normalize
from .ocr import DEFAULT_MAX_OCR_DOCUMENTS_PER_RUN, NeedsOCR
from .ocr import health_report as ocr_health
from .policy import HarvesterPolicy, enabled_sources
from .repository import CorpusRepository, ItemRecord, utcnow
from .rights import evaluate, get_profile
from .scheduler import BudgetExhausted, BudgetTracker, check_budget
from .security import SecurityError
from .selection import CorpusProfile, Curator
from .storage import CorpusStorage, mint_document_id

logger = logging.getLogger(__name__)

#: Corpus-harvester events, emitted onto the existing domain_events
#: fabric so a harvest is watchable on GET /events/stream like any other
#: operation. AGGREGATE_INGEST is reused rather than inventing a new
#: aggregate type: from the engine's point of view this *is* ingestion.
EVT_HARVEST_STARTED = "CorpusHarvestStarted"
EVT_HARVEST_SOURCE_COMPLETED = "CorpusHarvestSourceCompleted"
EVT_HARVEST_DOCUMENT_INGESTED = "CorpusDocumentIngested"
EVT_HARVEST_COMPLETED = "CorpusHarvestCompleted"
EVT_HARVEST_FAILED = "CorpusHarvestFailed"

MAX_RETRYABLE_ATTEMPTS = 3


#: One plan status, one document state. Written as a table rather than a
#: chain of ifs so that adding a status without deciding where its
#: documents rest is a KeyError here instead of a silent fall-through to
#: "download it anyway".
_PLAN_STATUS_TO_STATE = {
    PlanStatus.METADATA_ONLY: State.METADATA_ONLY,
    PlanStatus.AUTH_REQUIRED: State.AUTH_REQUIRED,
    PlanStatus.PROVIDER_UNVERIFIED: State.PROVIDER_UNVERIFIED,
    PlanStatus.CONTENT_UNAVAILABLE: State.CONTENT_UNAVAILABLE,
    PlanStatus.ACCESS_BLOCKED: State.ACCESS_BLOCKED,
    PlanStatus.OCR_PENDING: State.OCR_PENDING,
    PlanStatus.BULK_SNAPSHOT_PENDING: State.BULK_SNAPSHOT_PENDING,
}


@dataclass
class RunStats:
    """Counters for the run report. Every field appears in the JSON."""

    discovered: int = 0
    already_known: int = 0
    rights_accepted: int = 0
    rights_rejected: int = 0
    rights_quarantined: int = 0
    selected: int = 0
    downloaded: int = 0
    bytes_downloaded: int = 0
    normalized: int = 0
    quality_rejected: int = 0
    duplicates: int = 0
    classified_books: int = 0
    classified_manifesto: int = 0
    materialized: int = 0
    ingested: int = 0
    unchanged: int = 0
    #: Documents whose bytes are page images. Counted separately from
    #: errors on purpose: a scan waiting for a capability is not a
    #: failure, and folding it into the error count is what made Phase I
    #: report perfectly good documents as broken.
    ocr_pending: int = 0
    #: Documents the acquisition plan parked, by PlanStatus. Kept as a
    #: breakdown rather than a single number because "nobody hosts this"
    #: and "the provider refused us" call for entirely different actions.
    plan_parked: dict = field(default_factory=dict)
    errors: int = 0
    stopped_reason: str = ""
    stage_seconds: dict = field(default_factory=dict)
    per_source: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            name: getattr(self, name)
            for name in (
                "discovered", "already_known", "rights_accepted", "rights_rejected",
                "rights_quarantined", "selected", "downloaded", "bytes_downloaded",
                "normalized", "quality_rejected", "duplicates", "classified_books",
                "classified_manifesto", "materialized", "ingested", "unchanged",
                "ocr_pending", "plan_parked", "errors", "stopped_reason",
                "stage_seconds", "per_source",
            )
        }


class Orchestrator:
    """
    Drives one harvest run.

    Constructed with everything it needs injected -- config, policy,
    sources, fetcher -- so tests build one against fixtures and a
    temporary database with no monkeypatching.
    """

    def __init__(
        self,
        cfg: AppConfig,
        policy: HarvesterPolicy,
        sources: list[SourceConfig],
        *,
        fetcher: BaseFetcher | None = None,
        run_id: str | None = None,
        dry_run: bool = False,
        emit_events: bool = True,
    ) -> None:
        self.cfg = cfg
        self.policy = policy
        self.sources = sources
        self._registry: ProviderRegistry | None = None
        self.dry_run = dry_run
        self.run_id = run_id or f"corpus_{uuid.uuid4().hex[:16]}"
        self.repo = CorpusRepository(cfg.database)

        # Every data root, resolved absolutely and logged once. "Where is
        # it writing" should never require reading code -- and pilot
        # defect #8 was a path that resolved against the working
        # directory, so the same code wrote to different places depending
        # on where it was started.
        from .roots import DataRoots

        self.roots = DataRoots.from_config(cfg)
        self.roots.log_resolved()
        self.storage = CorpusStorage(self.roots.corpus)
        self.storage.ensure_layout()
        self.stats = RunStats()
        self.rights_profile = get_profile(policy.rights_profile)

        self.fetcher = fetcher or BoundedFetcher(
            policy.user_agent(),
            requests_per_minute=policy.budget.requests_per_minute,
            timeout=policy.budget.request_timeout,
            max_retries=policy.budget.max_retries,
            max_bytes=policy.budget.max_file_bytes,
            respect_robots=policy.respect_robots,
            cache=self.repo,
        )

        self.budget = BudgetTracker(
            budget=policy.budget,
            corpus_root=self.storage.root,
            existing_corpus_bytes=self.repo.total_corpus_bytes(),
        )

        self.curator = Curator(
            policy.selection_weights,
            seed=policy.selection_seed,
            exploration=policy.exploration,
            priority_languages=tuple(policy.languages),
        )

        self._emitter = (
            OperationEmitter(cfg, actor=ACTOR_CLI, aggregate_type=AGGREGATE_INGEST, correlation_id=self.run_id)
            if emit_events
            else None
        )

        # Structured per-run log at logs/corpus/<run_id>.jsonl. Separate
        # from domain_events on purpose: that fabric is the operational
        # event stream the whole engine shares, while this is the
        # harvester's own append-only trace, greppable without a database
        # and safe to ship to an operator. It records counts, ids,
        # hashes, and reasons -- never document text.
        from .reporting import RunLog

        self.log = RunLog(cfg, self.run_id)

    # ------------------------------------------------------------- helpers

    def _emit(self, event_type: str, payload: dict, aggregate_id=None) -> None:
        if self._emitter is None:
            return
        try:
            self._emitter.emit(event_type, aggregate_id=aggregate_id, payload=payload)
        except Exception as exc:
            # Event emission is observability, never correctness. A
            # failure here must not abort a harvest that is otherwise
            # succeeding -- the same best-effort discipline every other
            # write hook in this repository uses.
            logger.warning("Corpus event emission failed (%s): %s", event_type, exc)

    def _record_error(self, stage: str, exc: Exception, **keys) -> None:
        self.stats.errors += 1
        # `pipeline_stage`, not `stage`: RunLog.write's own first
        # parameter is called stage, so passing both collides.
        self.log.write("error", pipeline_stage=stage, error_type=type(exc).__name__,
                       message=str(exc)[:300], **keys)
        retryable = getattr(exc, "retryable", not isinstance(exc, SecurityError | NormalizationError))
        self.repo.record_error(
            stage=stage,
            error_type=type(exc).__name__,
            message=str(exc),
            retryable=bool(retryable),
            run_id=self.run_id,
            **keys,
        )

    # ----------------------------------------------------------- discovery

    def discover(self, profile: str = "broad") -> int:
        """
        Walk every enabled source's catalogue and record new candidates.

        Discovery is metadata-only and deliberately unbudgeted in bytes:
        the brief permits exhaustive discovery and budgets only
        acquisition. It is bounded per source by
        `max_candidates_per_source` so a single enormous catalogue cannot
        monopolise one run.
        """
        started = datetime.now(UTC)
        eligible = enabled_sources(self.sources, profile)
        if not eligible:
            logger.warning("No enabled sources for profile %r", profile)
            return 0

        for source in eligible:
            self.repo.upsert_source(
                source.source_id, source.adapter, source.display_name,
                source.enabled, source.base_url, source.trust,
                {"languages": source.languages, "notes": source.notes},
            )
            self._discover_one(source, profile)

        self.stats.stage_seconds["discovery"] = round((datetime.now(UTC) - started).total_seconds(), 2)
        return self.stats.discovered

    def _discover_one(self, source: SourceConfig, profile: str) -> None:
        cursor_before = self.repo.get_cursor(source.source_id)
        source_run_id = self.repo.start_source_run(self.run_id, source.source_id, cursor_before)
        # `seen` is every catalogue record walked; `discovered` is only
        # those not already known. Reporting the two as one number made a
        # re-run of an unchanged catalogue claim it had "discovered" its
        # entire contents again, which overstates every run report.
        counters = {"seen": 0, "discovered": 0, "already_known": 0,
                    "accepted": 0, "rejected": 0, "quarantined": 0}
        cursor_after = cursor_before
        error_message: str | None = None
        status = "completed"

        try:
            adapter = build_adapter(source, self.fetcher)
            # A per-source ceiling overrides the global one, and lowering
            # it is the only sane setting for a source whose discovery
            # costs a request PER CANDIDATE. Project Gutenberg has no
            # rights column in its catalogue, so each candidate needs its
            # own RDF fetch; at a polite 6 requests/minute the global
            # 500-candidate default would mean 80 minutes of discovery
            # for one source, which is not a bounded run by any reading.
            limit = int(
                source.option("max_candidates", self.policy.budget.max_candidates_per_source)
            )

            for candidate in adapter.discover(cursor_before, max_candidates=limit):
                counters["seen"] += 1
                self._record_candidate(candidate, counters)

            cursor_after = self.repo.get_cursor(source.source_id)
            self.repo.record_source_outcome(source.source_id, ok=True)

        except NotModified:
            # The catalogue has not changed since the last harvest. That
            # is a successful, zero-cost outcome, not a failure.
            status = "not_modified"
            self.repo.record_source_outcome(source.source_id, ok=True)
            logger.info("%s: catalogue unchanged since last run (304)", source.source_id)

        except Exception as exc:
            status = "failed"
            error_message = f"{type(exc).__name__}: {exc}"
            logger.warning("Discovery failed for %s: %s", source.source_id, error_message)
            self._record_error("discovery", exc, source_id=source.source_id)
            self.repo.record_source_outcome(source.source_id, ok=False)

        self.log.write("discovery", source_id=source.source_id, status=status, **counters)
        self.stats.discovered += counters["discovered"]
        self.stats.per_source.setdefault(source.source_id, {}).update(counters)
        self.stats.per_source[source.source_id]["status"] = status
        self.repo.finish_source_run(source_run_id, status, counters, cursor_after, error_message)
        self._emit(
            EVT_HARVEST_SOURCE_COMPLETED,
            {"source_id": source.source_id, "status": status, **counters},
        )

    def _record_candidate(self, candidate: Candidate, counters: dict) -> None:
        """
        Persist a candidate and evaluate its rights immediately.

        Rights are evaluated at discovery rather than at download for a
        reason: it means the pool the curator selects from is already
        legally filtered, so the selection score never has the chance to
        outweigh a legal problem.
        """
        candidate_id = mint_document_id(candidate.source_id, candidate.external_id)
        is_new = self.repo.upsert_candidate(candidate, candidate_id, self.run_id)
        if not is_new:
            counters["already_known"] += 1
            self.stats.already_known += 1
            return
        counters["discovered"] += 1

        source = next((s for s in self.sources if s.source_id == candidate.source_id), None)
        trusted = source.trusted_for_content_rights if source else True

        decision = evaluate(
            candidate.rights,
            self.rights_profile,
            source_trusted_for_content_rights=trusted,
            checked_at=utcnow(),
        )
        self.repo.record_rights_decision(decision, candidate_id=candidate_id, run_id=self.run_id)

        self.repo.set_candidate_state(candidate_id, State.RIGHTS_PENDING, "rights evaluation", self.run_id)

        if decision.decision == Decision.ACCEPT:
            counters["accepted"] += 1
            self.stats.rights_accepted += 1
            self.repo.set_candidate_state(
                candidate_id, State.RIGHTS_ACCEPTED,
                f"accepted under {decision.normalized_license}", self.run_id,
                [c.value for c in decision.reason_codes],
            )
        elif decision.decision == Decision.REJECT:
            counters["rejected"] += 1
            self.stats.rights_rejected += 1
            self.repo.set_candidate_state(
                candidate_id, State.RIGHTS_REJECTED, "rights rejected", self.run_id,
                [c.value for c in decision.reason_codes],
            )
        else:
            counters["quarantined"] += 1
            self.stats.rights_quarantined += 1
            self.repo.set_candidate_state(
                candidate_id, State.RIGHTS_QUARANTINED, "rights quarantined", self.run_id,
                [c.value for c in decision.reason_codes],
            )
            self.storage.write_quarantine(
                candidate_id,
                {
                    "candidate_id": candidate_id,
                    "source_id": candidate.source_id,
                    "external_id": candidate.external_id,
                    "title": candidate.title,
                    "canonical_url": candidate.canonical_url,
                    "quarantined_at": utcnow(),
                    "decision": decision.to_dict(),
                },
            )

    # ------------------------------------------------------------ planning

    def plan(self, limit: int | None = None) -> list[dict]:
        """
        Rank rights-accepted candidates and return what a sync would
        acquire. Writes nothing but selection scores; safe to run at any
        time, and `sync` records a plan before its first real acquisition.
        """
        started = datetime.now(UTC)
        rows = self.repo.candidates_in_state(State.RIGHTS_ACCEPTED)
        if not rows:
            return []

        candidates = [_candidate_from_row(row) for row in rows]

        # Partition by whether ANY provider can serve the bytes.
        #
        # This used to be `[c for c in candidates if c.download_url]` --
        # the Phase I assumption one layer above the download, silently
        # discarding every document a provider described but does not
        # host. Those candidates are real, rights-assessed, and worth
        # recording; they simply must not compete for a download budget
        # they will never spend.
        acquirable: list[Candidate] = []
        parked: list[tuple[Candidate, AcquisitionPlan]] = []
        for candidate in candidates:
            source = next(
                (s for s in self.sources if s.source_id == candidate.source_id), None
            )
            if source is None:
                continue
            plan_for_candidate = self._plan_for(candidate, source)
            if plan_for_candidate.is_actionable():
                acquirable.append(candidate)
            else:
                parked.append((candidate, plan_for_candidate))
        candidates = acquirable

        profile = CorpusProfile.from_items(self.repo.all_items())
        trust = {s.source_id: s.trust for s in self.sources}

        target = limit if limit is not None else self.policy.budget.max_items_per_run
        ranked = self.curator.rank(candidates, profile, source_trust=trust, limit=target)

        plan: list[dict] = []
        for scored in ranked:
            candidate_id = mint_document_id(scored.candidate.source_id, scored.candidate.external_id)
            self.repo.set_candidate_selection(candidate_id, scored.score, scored.rationale)
            plan.append(
                {
                    "candidate_id": candidate_id,
                    "source_id": scored.candidate.source_id,
                    "external_id": scored.candidate.external_id,
                    "title": scored.candidate.title,
                    "authors": list(scored.candidate.authors),
                    "language": scored.candidate.language,
                    "format": scored.candidate.download_format,
                    "estimated_bytes": scored.candidate.estimated_bytes,
                    "canonical_url": scored.candidate.canonical_url,
                    "score": scored.score,
                    "components": scored.components,
                    "rationale": scored.rationale,
                }
            )

        # Parked entries ride along outside the ranking and outside the
        # item budget: they cost no bandwidth, and dropping them here is
        # what made a discovery-only source look like a broken one.
        for candidate, parked_plan in parked:
            plan.append(
                {
                    "candidate_id": mint_document_id(
                        candidate.source_id, candidate.external_id
                    ),
                    "source_id": candidate.source_id,
                    "external_id": candidate.external_id,
                    "title": candidate.title,
                    "authors": list(candidate.authors),
                    "language": candidate.language,
                    "format": candidate.download_format,
                    "estimated_bytes": 0,
                    "canonical_url": candidate.canonical_url,
                    "score": 0.0,
                    "components": {},
                    "rationale": parked_plan.reason,
                    "plan_status": parked_plan.status.value,
                }
            )

        self.stats.stage_seconds["planning"] = round((datetime.now(UTC) - started).total_seconds(), 2)
        self.log.write(
            "plan", entries=len(plan), parked=len(parked),
            top=[{"id": e["candidate_id"], "score": e["score"]} for e in plan[:10]],
        )
        return plan

    # -------------------------------------------------------- acquisition

    def acquire(self, plan: list[dict]) -> None:
        """
        Download, normalize, deduplicate, and classify each planned
        candidate, stopping cleanly when a budget is reached.
        """
        started = datetime.now(UTC)

        for entry in plan:
            candidate_row = self.repo.get_candidate(entry["candidate_id"])
            if candidate_row is None:
                continue
            candidate = _candidate_from_row(candidate_row)

            try:
                self.budget.check_can_download(candidate.estimated_bytes)
            except BudgetExhausted as exc:
                self.stats.stopped_reason = f"{exc.limit_name}: {exc}"
                self.budget.stopped_reason = self.stats.stopped_reason
                logger.info("Stopping acquisition cleanly: %s", exc)
                break

            self.stats.selected += 1
            try:
                self._acquire_one(candidate, entry["candidate_id"])
            except BudgetExhausted as exc:
                self.stats.stopped_reason = f"{exc.limit_name}: {exc}"
                break
            except Exception as exc:
                logger.warning("Acquisition failed for %s: %s", candidate.document_key(), exc)
                self._record_error("acquisition", exc, candidate_id=entry["candidate_id"])

        self.stats.stage_seconds["acquisition"] = round((datetime.now(UTC) - started).total_seconds(), 2)

    def _acquire_one(self, candidate: Candidate, candidate_id: str) -> None:
        document_id = mint_document_id(candidate.source_id, candidate.external_id)

        existing = self.repo.get_item(document_id)
        if existing is not None and existing.state in (
            State.INGESTED, State.INDEXED, State.MATERIALIZED, State.DUPLICATE,
            State.QUALITY_REJECTED, State.CLASSIFIED_BOOKS, State.CLASSIFIED_MANIFESTO,
        ):
            # Already acquired in a previous run. This is the branch that
            # makes a second identical run download nothing.
            self.stats.unchanged += 1
            return

        source = next((s for s in self.sources if s.source_id == candidate.source_id), None)
        if source is None:
            raise ValueError(f"No configuration for source {candidate.source_id!r}")

        self.repo.create_item(document_id, candidate, candidate_id, self.run_id)

        decision = self.repo.latest_rights_decision(document_id)
        if not decision:
            # The decision was recorded against the candidate; copy it
            # onto the item so the document carries its own dossier.
            candidate_decision = self.repo.rights_decisions(document_id)
            if not candidate_decision:
                self._copy_candidate_decision(candidate_id, document_id)

        item = self.repo.get_item(document_id)
        assert item is not None
        if item.state == State.RIGHTS_PENDING:
            self.repo.transition(
                document_id, State.RIGHTS_ACCEPTED, "rights accepted at discovery", self.run_id
            )

        # ---- plan ----
        #
        # Planning sits between rights acceptance and queueing, because
        # "may we use this?" and "can anyone actually serve it?" are
        # different questions and the second one has more than two
        # answers. A document reaches QUEUED only once the graph says
        # some provider fills the content role.
        #
        # Before this existed, a source that declared no content role
        # produced a stream of download failures instead of
        # METADATA_ONLY records -- the Phase I behaviour the whole graph
        # was built to remove.
        plan = self._plan_for(candidate, source)
        self.repo.record_plan(document_id, plan.to_dict(), self.run_id)

        if not plan.is_actionable():
            state = _PLAN_STATUS_TO_STATE[plan.status]
            self.repo.transition(document_id, state, plan.reason, self.run_id)
            self.stats.plan_parked[plan.status.value] = (
                self.stats.plan_parked.get(plan.status.value, 0) + 1
            )
            if plan.is_error():
                self.stats.errors += 1
            logger.info("%s: %s -- %s", document_id, plan.status.value, plan.reason)
            return

        self.repo.transition(document_id, State.QUEUED, "selected by curator", self.run_id)

        if self.dry_run:
            return

        # ---- download ----
        self.repo.transition(document_id, State.DOWNLOADING, "download started", self.run_id)
        adapter = build_adapter(source, self.fetcher)
        try:
            response = adapter.fetch_content(candidate, max_bytes=self.policy.budget.max_file_bytes)
        except (FetchError, SecurityError, ValueError) as exc:
            self._fail_document(document_id, "download", exc)
            return

        self.budget.record_download(len(response.content))
        self.stats.downloaded += 1
        self.stats.bytes_downloaded += len(response.content)

        digest, blob_path, was_new = self.storage.store_blob(response.content, digest=response.sha256)
        self.repo.record_artifact(
            document_id, "raw", digest, str(blob_path), len(response.content), response.detected_mime, response.encoding
        )
        self.repo.transition(
            document_id, State.DOWNLOADED, "download complete", self.run_id,
            updates={
                "raw_sha256": digest,
                "size_bytes": len(response.content),
                "detected_mime": response.detected_mime,
                "declared_mime": response.declared_mime,
                "content_url": response.final_url,
                "downloaded_at": utcnow(),
                "source_format": response.detected_mime,
            },
        )

        # ---- normalize ----
        try:
            normalized = normalize(response.content, response.detected_mime, response.encoding)
        except NeedsOCR as exc:
            # Page images are not a broken document. The bytes are stored,
            # the rights decision stands, and the document waits in a
            # non-error state until an OCR backend exists -- at which
            # point it becomes processable without re-downloading
            # anything. Phase I recorded exactly this as a failure.
            health = ocr_health(self.policy.ocr_enabled, self._max_ocr_documents())
            self.repo.transition(
                document_id, State.OCR_PENDING,
                f"{exc} ({health['reason'] or 'a backend is available'})",
                self.run_id,
            )
            self.stats.ocr_pending += 1
            logger.info("document %s needs OCR: %s", document_id, exc)
            return
        except (NormalizationError, SecurityError) as exc:
            self._fail_document(document_id, "normalization", exc, final=True)
            return

        norm_digest, norm_path, _ = self.storage.store_normalized(normalized.text)
        self.repo.record_artifact(
            document_id, "normalized", norm_digest, str(norm_path), len(normalized.text.encode("utf-8")),
            "text/plain", "utf-8",
        )

        license_sha = ""
        if normalized.license_text:
            license_sha, license_path = self.storage.store_license(normalized.license_text)
            self.repo.record_artifact(
                document_id, "license", license_sha, str(license_path),
                len(normalized.license_text.encode("utf-8")), "text/plain", "utf-8",
            )

        self.repo.transition(
            document_id, State.NORMALIZED, f"normalized from {normalized.source_format}", self.run_id,
            updates={
                "normalized_sha256": norm_digest,
                "normalized_chars": len(normalized.text),
                "source_format": normalized.source_format,
                "normalizer_version": NORMALIZER_VERSION,
                "license_original_text": (normalized.license_text or "")[:8000],
                "license_evidence_sha256": license_sha,
            },
        )
        self.stats.normalized += 1

        # ---- quality ----
        report = quality_mod.score_quality(
            normalized.text,
            declared_language=candidate.language,
            document_type=candidate.document_type,
            source_format=normalized.source_format,
            format_metrics=normalized.metrics,
            thresholds=self.policy.quality,
        )
        self.repo.update_item(
            document_id,
            quality_score=report.score,
            quality_report_json=json.dumps(report.to_dict(), ensure_ascii=False),
            language=report.language_detected if report.language_detected != "unknown" else candidate.language,
        )

        if not quality_mod.passes(report, self.policy.quality):
            self.repo.transition(
                document_id, State.QUALITY_REJECTED,
                f"quality {report.score:.2f} below threshold: {', '.join(report.issues) or 'unspecified'}",
                self.run_id, list(report.issues),
            )
            self.stats.quality_rejected += 1
            return

        # ---- deduplicate ----
        item = self.repo.get_item(document_id)
        assert item is not None
        verdict = self._deduplicate(item, normalized.text, candidate)
        if verdict is not None and verdict.is_duplicate:
            self.repo.transition(
                document_id, State.DUPLICATE,
                f"{verdict.kind.value if verdict.kind else 'duplicate'}: {verdict.reason}",
                self.run_id, updates={"duplicate_of": verdict.matched_document_id, "duplicate_score": verdict.similarity},
            )
            self.stats.duplicates += 1
            return

        # ---- classify ----
        result = classification_mod.classify(
            title=candidate.title,
            document_type=candidate.document_type,
            subjects=list(candidate.subjects),
            text=normalized.text,
            structure=normalized.structure,
            cfg=self.cfg,
            use_llm=self.policy.use_llm_classifier,
            model=self.policy.classifier_model or None,
            threshold=self.policy.manifesto_threshold,
        )
        self.repo.record_classification(document_id, result, self.run_id)

        target_state = (
            State.CLASSIFIED_MANIFESTO if result.destination == Destination.MANIFESTO else State.CLASSIFIED_BOOKS
        )
        self.log.write(
            "classified", document_id=document_id, destination=result.destination.value,
            manifesto_score=result.manifesto_score, confidence=result.confidence,
            low_confidence=result.low_confidence, classifier=result.classifier_kind,
        )
        self.repo.transition(
            document_id, target_state, result.rationale[:400], self.run_id,
            updates={
                "destination": result.destination.value,
                "manifesto_score": result.manifesto_score,
                "classification_confidence": result.confidence,
                "classification_low_confidence": int(result.low_confidence),
                "classifier_version": result.classifier_version,
                "classifier_kind": result.classifier_kind,
            },
        )
        if result.destination == Destination.MANIFESTO:
            self.stats.classified_manifesto += 1
        else:
            self.stats.classified_books += 1

        self._record_attributions(document_id, candidate)

    def _copy_candidate_decision(self, candidate_id: str, document_id: str) -> None:
        """
        Re-record the candidate's rights decision against the document.

        The decision is re-recorded rather than moved: the candidate-level
        row remains as the record of what was decided at discovery, and
        the document gets its own dossier. Both are append-only.
        """
        from .models import Evidence, RightsDecision

        with __import__("sqlite3").connect(self.cfg.database) as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT * FROM corpus_rights_decisions WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
        if row is None:
            return

        data = dict(row)
        decision = RightsDecision(
            decision=Decision(data["decision"]),
            normalized_license=data["normalized_license"],
            rights_scope=DistributionScope(data["rights_scope"]),
            commercial_use=bool(data["commercial_use"]),
            redistribution=bool(data["redistribution"]),
            derivatives=bool(data["derivatives"]),
            attribution_required=bool(data["attribution_required"]),
            share_alike=bool(data["share_alike"]),
            jurisdictions=tuple(json.loads(data["jurisdictions"] or "[]")),
            evidence=tuple(Evidence(**e) for e in json.loads(data["evidence_json"] or "[]")),
            reason_codes=tuple(ReasonCode(c) for c in json.loads(data["reason_codes"] or "[]")),
            policy_profile=data["policy_profile"],
            policy_version=data["policy_version"],
            evidence_checked_at=data["evidence_checked_at"] or utcnow(),
            notes=data["notes"] or "",
        )
        self.repo.record_rights_decision(decision, document_id=document_id, run_id=self.run_id)

        evidence_url = decision.evidence[0].url if decision.evidence else ""
        self.repo.update_item(
            document_id,
            rights_status=State.RIGHTS_ACCEPTED.value if decision.decision == Decision.ACCEPT else data["decision"],
            normalized_license=decision.normalized_license,
            license_evidence_url=evidence_url,
            rights_verified_at=decision.evidence_checked_at,
            jurisdictions=json.dumps(list(decision.jurisdictions)),
            distribution_scope=decision.rights_scope.value,
            attribution_required=int(decision.attribution_required),
            share_alike=int(decision.share_alike),
        )

    def _deduplicate(self, item: ItemRecord, text: str, candidate: Candidate):
        fingerprint = dedup.DocumentFingerprint(
            document_id=item.document_id,
            raw_sha256=item.raw_sha256 or "",
            normalized_sha256=item.normalized_sha256 or "",
            simhash_value=dedup.simhash(text),
            title=candidate.title,
            authors=candidate.authors,
            language=item.language or candidate.language,
            translator=candidate.translator,
            identifiers=dedup.extract_identifiers(
                candidate.canonical_url, candidate.download_url, *candidate.work_identifiers
            )
            | set(candidate.work_identifiers),
            char_count=len(text),
            quality_score=item.quality_score or 0.0,
        )
        self.repo.update_item(item.document_id, simhash=str(fingerprint.simhash_value))

        corpus = [
            dedup.DocumentFingerprint(
                document_id=row["document_id"],
                raw_sha256=row.get("raw_sha256") or "",
                normalized_sha256=row.get("normalized_sha256") or "",
                simhash_value=int(row["simhash"]) if (row.get("simhash") or "").lstrip("-").isdigit() else 0,
                title=row.get("title") or "",
                authors=tuple(json.loads(row.get("authors") or "[]")),
                language=row.get("language") or "",
                translator=row.get("translator") or "",
                edition_id=row.get("edition_id") or "",
                char_count=int(row.get("normalized_chars") or 0),
            )
            for row in self.repo.indexed_fingerprints(exclude_document_id=item.document_id)
        ]
        if not corpus:
            return None

        verdict = dedup.find_duplicate(fingerprint, corpus)
        if verdict.is_duplicate and verdict.kind:
            cluster = dedup.cluster_id_for([item.document_id, verdict.matched_document_id])
            self.repo.record_duplicate(
                cluster, verdict.kind.value, verdict.matched_document_id, item.document_id, verdict.similarity
            )
        return verdict

    def _record_attributions(self, document_id: str, candidate: Candidate) -> None:
        for author in candidate.authors:
            self.repo.record_attribution(document_id, "author", author)
        for contributor in candidate.contributors:
            self.repo.record_attribution(document_id, "contributor", contributor)
        if candidate.translator:
            self.repo.record_attribution(document_id, "translator", candidate.translator)
        source = next((s for s in self.sources if s.source_id == candidate.source_id), None)
        self.repo.record_attribution(
            document_id, "source", source.display_name if source else candidate.source_id,
            candidate.canonical_url,
        )

    def provider_registry(self) -> ProviderRegistry:
        """
        Every configured source's declared capabilities, built once.

        Credential presence is resolved here and only as a boolean, so a
        token's *value* never enters the registry and cannot reach a plan
        record or a log line.
        """
        if self._registry is None:
            registry = ProviderRegistry()
            for source in self.sources:
                adapter_cls = ADAPTER_REGISTRY.get(source.adapter)
                if adapter_cls is None:
                    continue
                registry.register(adapter_cls.declared_capabilities(source.source_id))
            self._registry = registry
        return self._registry

    def _plan_for(self, candidate: Candidate, source: SourceConfig) -> AcquisitionPlan:
        """
        Which providers fill which roles for this one document.

        Today a candidate names at most its own source as a content host,
        so most plans are single-provider -- but the decision now runs
        through the graph rather than through `if candidate.download_url`,
        which is what lets a source declare that it does not host content
        and have that mean something.
        """
        content_candidates: tuple[tuple[str, str], ...] = ()
        if candidate.download_url:
            content_candidates = ((source.source_id, candidate.download_url),)

        raw = candidate.raw_metadata or {}
        return build_plan(
            candidate.document_key(),
            self.provider_registry(),
            discovered_by=source.source_id,
            metadata_from=source.source_id,
            rights_evidence_from=source.source_id,
            content_candidates=content_candidates,
            expected_format=candidate.download_format,
            allowed_hosts_by_provider={source.source_id: source.allowed_hosts},
            bulk_pending=bool(raw.get("bulk_snapshot_pending")),
        )

    def _max_ocr_documents(self) -> int:
        return int(
            getattr(self.policy, "max_ocr_documents_per_run", DEFAULT_MAX_OCR_DOCUMENTS_PER_RUN)
        )

    def _fail_document(self, document_id: str, stage: str, exc: Exception, *, final: bool = False) -> None:
        """
        Route a failure to FAILED_RETRYABLE or FAILED_FINAL.

        A document that has already failed the same stage
        MAX_RETRYABLE_ATTEMPTS times becomes final regardless of the
        error's own retryability -- otherwise a permanently broken URL
        that happens to time out would be retried forever.
        """
        attempts = self.repo.attempt_count(document_id, stage)
        retryable = getattr(exc, "retryable", True) and not isinstance(exc, SecurityError | NormalizationError)
        is_final = final or not retryable or attempts + 1 >= MAX_RETRYABLE_ATTEMPTS

        self._record_error(stage, exc, document_id=document_id)
        self.repo.transition(
            document_id,
            State.FAILED_FINAL if is_final else State.FAILED_RETRYABLE,
            f"{stage}: {type(exc).__name__}: {exc}"[:400],
            self.run_id,
            error={"stage": stage, "type": type(exc).__name__, "message": str(exc)[:500], "attempt": attempts + 1},
        )

    # -------------------------------------------------- materialize/ingest

    def materialize_and_ingest(self) -> None:
        """Move classified documents into the corpus and index them."""
        started = datetime.now(UTC)

        pending = self.repo.items_in_states([State.CLASSIFIED_BOOKS, State.CLASSIFIED_MANIFESTO])
        for item in pending:
            try:
                result = materialize(self.cfg, self.repo, self.storage, item)
                self.repo.transition(
                    item.document_id, State.MATERIALIZED,
                    f"materialized ({result.link_mode})", self.run_id,
                    updates={"materialized_path": str(result.path)},
                )
                self.stats.materialized += 1
            except Exception as exc:
                logger.warning("Materialization failed for %s: %s", item.document_id, exc)
                self._record_error("materialization", exc, document_id=item.document_id)

        for item in self.repo.items_in_states([State.MATERIALIZED]):
            try:
                outcome = ingest_document(self.cfg, self.repo, item)
                if outcome.status in ("ingested", "reingested"):
                    self.repo.transition(
                        item.document_id, State.INGESTED,
                        f"{outcome.status}: {outcome.chunk_count} chunks", self.run_id,
                        updates={"source_row_id": outcome.source_id, "ingested_at": utcnow(),
                                 "chunker_version": "fieldhorizon.ingest.chunk_text/1"},
                    )
                    self.repo.transition(
                        item.document_id, State.INDEXED, "present in chunks_fts", self.run_id,
                        updates={"indexed_at": utcnow()},
                    )
                    self.stats.ingested += 1
                    self.log.write(
                        "ingested", document_id=item.document_id, chunks=outcome.chunk_count,
                        destination=item.destination.value if item.destination else "",
                        license=item.normalized_license, scope=item.distribution_scope,
                    )
                    self._emit(
                        EVT_HARVEST_DOCUMENT_INGESTED,
                        {
                            "document_id": item.document_id,
                            "title": item.title,
                            "destination": item.destination.value if item.destination else "",
                            "chunks": outcome.chunk_count,
                            "license": item.normalized_license,
                        },
                        aggregate_id=outcome.source_id,
                    )
                elif outcome.status == "unchanged":
                    self.stats.unchanged += 1
                    self.repo.transition(
                        item.document_id, State.INGESTED, "already ingested, text unchanged", self.run_id,
                        updates={"source_row_id": outcome.source_id},
                    )
                    self.repo.transition(item.document_id, State.INDEXED, "already indexed", self.run_id)
                else:
                    logger.info("Ingestion skipped for %s: %s", item.document_id, outcome.reason)
            except Exception as exc:
                logger.warning("Ingestion failed for %s: %s", item.document_id, exc)
                self._record_error("ingestion", exc, document_id=item.document_id)

        self.stats.stage_seconds["materialization"] = round((datetime.now(UTC) - started).total_seconds(), 2)

    # ----------------------------------------------------------- full runs

    def run_sync(self, profile: str = "broad", limit: int | None = None) -> RunStats:
        """
        A complete bounded cycle: discover, plan, acquire, materialize,
        ingest.

        The plan is always written to disk before the first acquisition
        (the brief's "la première exécution réelle doit être précédée
        automatiquement d'un plan ou dry-run enregistré"), so what a run
        intended is recoverable even if it dies during acquisition.
        """
        check_budget(self.policy.budget, self.storage.root, profile, autonomous=not self.dry_run)

        plan_path = self.storage.reports / f"{self.run_id}_plan.json"
        self.repo.start_run(self.run_id, profile, self.policy.rights_profile, "sync", self.dry_run, str(plan_path))
        self._emit(EVT_HARVEST_STARTED, {"profile": profile, "rights_profile": self.policy.rights_profile,
                                         "dry_run": self.dry_run})

        try:
            self.discover(profile)
            plan = self.plan(limit)

            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(
                json.dumps(
                    {"run_id": self.run_id, "profile": profile, "generated_at": utcnow(),
                     "rights_profile": self.policy.rights_profile, "entries": plan},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )

            if not self.dry_run:
                self.acquire(plan)
                self.materialize_and_ingest()

            self.repo.finish_run(self.run_id, "completed", self.stats.to_dict())
            self.log.write("completed", **self.stats.to_dict())
            self._emit(EVT_HARVEST_COMPLETED, self.stats.to_dict())

        except Exception as exc:
            self.repo.finish_run(self.run_id, "failed", self.stats.to_dict(), error=f"{type(exc).__name__}: {exc}")
            self._emit(EVT_HARVEST_FAILED, {"error_type": type(exc).__name__, "error_message": str(exc)})
            raise

        return self.stats

    def resume(self) -> RunStats:
        """
        Continue whatever an interrupted run left behind.

        Reads state from the database and runs the stage each document's
        state implies. Documents stuck in DOWNLOADING or QUEUED are
        re-acquired; FAILED_RETRYABLE documents are retried up to their
        attempt limit; classified-but-unmaterialized documents are
        finished off.
        """
        check_budget(self.policy.budget, self.storage.root, self.policy.profile, autonomous=True)
        self.repo.start_run(self.run_id, self.policy.profile, self.policy.rights_profile, "resume", False)
        self._emit(EVT_HARVEST_STARTED, {"mode": "resume"})

        try:
            stuck = self.repo.items_in_states(
                [State.QUEUED, State.DOWNLOADING, State.FAILED_RETRYABLE, State.DOWNLOADED, State.NORMALIZED]
            )
            for item in stuck:
                candidate_row = self.repo.find_candidate(item.source_id, item.external_id)
                if candidate_row is None:
                    continue
                candidate = _candidate_from_row(candidate_row)
                if item.state in (State.DOWNLOADING, State.FAILED_RETRYABLE, State.QUEUED):
                    self.repo.transition(item.document_id, State.QUEUED, "resumed", self.run_id)
                try:
                    self.budget.check_can_download(candidate.estimated_bytes)
                except BudgetExhausted as exc:
                    self.stats.stopped_reason = f"{exc.limit_name}: {exc}"
                    break
                try:
                    self._acquire_one(candidate, item.row.get("candidate_id") or item.document_id)
                    self.stats.selected += 1
                except Exception as exc:
                    self._record_error("resume", exc, document_id=item.document_id)

            self.materialize_and_ingest()
            self.repo.finish_run(self.run_id, "completed", self.stats.to_dict())
            self._emit(EVT_HARVEST_COMPLETED, self.stats.to_dict())
        except Exception as exc:
            self.repo.finish_run(self.run_id, "failed", self.stats.to_dict(), error=str(exc))
            self._emit(EVT_HARVEST_FAILED, {"error_type": type(exc).__name__, "error_message": str(exc)})
            raise

        return self.stats

    # ----------------------------------------------------- renormalization

    def renormalize(self, *, reclassify: bool = True) -> dict:
        """
        Rebuild normalized text, quality, and classification from the
        stored raw blobs. No network access.

        This is what makes the immutable raw store worth keeping: when
        the normalizer improves -- a boilerplate pattern learned, an
        apparatus page identified -- the existing corpus can be brought
        up to the new behaviour without re-downloading anything or asking
        a provider for the same bytes twice.

        Rights are NOT re-decided here. Normalization has no bearing on
        whether a document may be held, and silently re-running a rights
        decision under the guise of a text refresh would be exactly the
        kind of implicit re-adjudication this system avoids. Use
        `audit-rights` for that.
        """
        results: dict[str, Any] = {"examined": 0, "renormalized": 0, "unchanged": 0, "failed": 0, "details": []}

        for item in self.repo.items_in_states(
            [State.INGESTED, State.INDEXED, State.MATERIALIZED,
             State.CLASSIFIED_BOOKS, State.CLASSIFIED_MANIFESTO]
        ):
            if not item.raw_sha256:
                continue
            results["examined"] += 1

            blob = self.storage.blob_path(item.raw_sha256)
            if not blob.exists():
                results["failed"] += 1
                results["details"].append({"document_id": item.document_id, "reason": "raw blob missing"})
                continue

            try:
                data = blob.read_bytes()
                mime = item.row.get("detected_mime") or ""
                normalized = normalize(data, mime)
            except Exception as exc:
                results["failed"] += 1
                results["details"].append({"document_id": item.document_id, "reason": f"{type(exc).__name__}: {exc}"})
                self._record_error("renormalize", exc, document_id=item.document_id)
                continue

            digest, path, _ = self.storage.store_normalized(normalized.text)
            if digest == item.normalized_sha256:
                results["unchanged"] += 1
                continue

            self.repo.record_artifact(
                item.document_id, "normalized", digest, str(path),
                len(normalized.text.encode("utf-8")), "text/plain", "utf-8",
            )

            report = quality_mod.score_quality(
                normalized.text,
                declared_language=item.language,
                document_type=item.row.get("document_type") or "",
                source_format=normalized.source_format,
                format_metrics=normalized.metrics,
                thresholds=self.policy.quality,
            )
            updates = {
                "normalized_sha256": digest,
                "normalized_chars": len(normalized.text),
                "normalizer_version": NORMALIZER_VERSION,
                "quality_score": report.score,
                "quality_report_json": json.dumps(report.to_dict(), ensure_ascii=False),
                "simhash": str(dedup.simhash(normalized.text)),
            }
            if normalized.license_text:
                license_sha, license_path = self.storage.store_license(normalized.license_text)
                self.repo.record_artifact(
                    item.document_id, "license", license_sha, str(license_path),
                    len(normalized.license_text.encode("utf-8")), "text/plain", "utf-8",
                )
                updates["license_original_text"] = normalized.license_text[:8000]
                updates["license_evidence_sha256"] = license_sha

            if reclassify:
                result = classification_mod.classify(
                    title=item.title,
                    document_type=item.row.get("document_type") or "",
                    subjects=json.loads(item.row.get("subjects") or "[]"),
                    text=normalized.text,
                    structure=normalized.structure,
                    cfg=self.cfg,
                    use_llm=self.policy.use_llm_classifier,
                    model=self.policy.classifier_model or None,
                    threshold=self.policy.manifesto_threshold,
                )
                self.repo.record_classification(item.document_id, result, self.run_id)
                updates.update(
                    destination=result.destination.value,
                    manifesto_score=result.manifesto_score,
                    classification_confidence=result.confidence,
                    classification_low_confidence=int(result.low_confidence),
                    classifier_version=result.classifier_version,
                    classifier_kind=result.classifier_kind,
                )

            self.repo.update_item(item.document_id, **updates)
            results["renormalized"] += 1
            results["details"].append(
                {
                    "document_id": item.document_id,
                    "title": item.title,
                    "chars_before": item.row.get("normalized_chars"),
                    "chars_after": len(normalized.text),
                }
            )
            self.log.write("renormalized", document_id=item.document_id,
                           chars=len(normalized.text), quality=report.score)

        return results

    # ------------------------------------------------------- rights audit

    def audit_rights(self, stale_after_days: int = 30) -> dict:
        """
        Re-verify the rights of documents already in the corpus.

        Two things can happen to a document that was legitimately
        accepted: its evidence goes stale (nobody has re-checked in N
        days), or the provider changes its statement. The first marks it
        STALE and pulls it from the active index; the second is caught
        the next time discovery refreshes the candidate's rights signal
        and re-evaluates.

        In both cases the file, the blob, the sidecar, and every prior
        decision row are preserved. Withdrawal removes a document from
        the index, never from the record.
        """
        cutoff = datetime.now(UTC) - timedelta(days=stale_after_days)
        results: dict[str, Any] = {"checked": 0, "stale": 0, "withdrawn": 0, "still_valid": 0, "details": []}

        for item in self.repo.items_in_states([State.INGESTED, State.INDEXED, State.MATERIALIZED]):
            results["checked"] += 1
            decision = self.repo.latest_rights_decision(item.document_id)

            if not decision or decision.get("decision") != "accept":
                # Should be unreachable given the ingest gate, and is
                # checked anyway: if it ever happens, the document leaves
                # the index immediately.
                self._withdraw(item, "no accepted rights decision on record")
                results["withdrawn"] += 1
                results["details"].append({"document_id": item.document_id, "action": "withdrawn",
                                           "reason": "no accepted rights decision"})
                continue

            checked_at = decision.get("evidence_checked_at") or ""
            try:
                checked = datetime.fromisoformat(checked_at)
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                checked = None

            if checked is None or checked < cutoff:
                self.repo.transition(
                    item.document_id, State.STALE,
                    f"licence evidence not re-verified since {checked_at or 'never'}",
                    self.run_id, [ReasonCode.EVIDENCE_STALE.value],
                )
                withdraw_document(self.cfg, item.document_id)
                results["stale"] += 1
                results["details"].append(
                    {"document_id": item.document_id, "action": "marked_stale", "last_checked": checked_at}
                )
            else:
                results["still_valid"] += 1

        return results

    def _withdraw(self, item: ItemRecord, reason: str) -> None:
        withdraw_document(self.cfg, item.document_id)
        dematerialize(self.cfg, item)
        self.repo.transition(item.document_id, State.WITHDRAWN, reason, self.run_id)


def _candidate_from_row(row: dict) -> Candidate:
    """Rebuild a Candidate from its persisted corpus_candidates row."""
    from .models import Evidence, RightsSignal

    signal_data = json.loads(row.get("rights_signal_json") or "{}")
    raw_metadata = json.loads(row.get("raw_metadata_json") or "{}")

    evidence = tuple(
        Evidence(
            kind=e.get("kind", ""), value=e.get("value", ""), url=e.get("url", ""),
            content_hash=e.get("content_hash", ""), retrieved_at=e.get("retrieved_at", ""),
        )
        for e in signal_data.get("evidence", [])
        if isinstance(e, dict)
    )

    downloads = raw_metadata.get("downloads") or []
    alternates = tuple(
        (str(d.get("format", "")), str(d.get("url", "")))
        for d in downloads[1:]
        if isinstance(d, dict) and d.get("url")
    )

    return Candidate(
        source_id=row["source_id"],
        external_id=row["external_id"],
        title=row.get("title") or "",
        authors=tuple(json.loads(row.get("authors") or "[]")),
        language=row.get("language") or "",
        publication_date=str(raw_metadata.get("publication_date") or ""),
        document_type=row.get("document_type") or "",
        subjects=tuple(json.loads(row.get("subjects") or "[]")),
        canonical_url=row.get("canonical_url") or "",
        download_url=row.get("download_url") or "",
        download_format=row.get("download_format") or "",
        estimated_bytes=int(row.get("estimated_bytes") or 0),
        alternate_downloads=alternates,
        rights=RightsSignal(
            content_license=signal_data.get("content_license", ""),
            metadata_license=signal_data.get("metadata_license", ""),
            rights_statement_uri=signal_data.get("rights_statement_uri", ""),
            provider_declared_scope=signal_data.get("provider_declared_scope", ""),
            jurisdictions=tuple(signal_data.get("jurisdictions", [])),
            evidence=evidence,
            access_restricted=bool(signal_data.get("access_restricted")),
            borrow_only=bool(signal_data.get("borrow_only")),
            uploader_asserted_only=bool(signal_data.get("uploader_asserted_only")),
            raw_rights_text=signal_data.get("raw_rights_text", ""),
        ),
        raw_metadata=raw_metadata,
    )


__all__ = ["EVT_HARVEST_COMPLETED", "EVT_HARVEST_STARTED", "Orchestrator", "RunStats"]
