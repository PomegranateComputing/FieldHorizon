from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

import fieldhorizon

from .concepts import SEMANTIC_KINDS
from .config import AppConfig, RetrievalWeights
from .contracts import (
    BeliefsResponse,
    CanonCandidateModel,
    CanonEntryModel,
    CanonResponse,
    CapabilitiesResponse,
    CapabilityModel,
    ChunkModel,
    ChunksResponse,
    ConfigResponse,
    ContradictionsResponse,
    CouncilCaseModel,
    CouncilDetailResponse,
    CouncilModel,
    CouncilsResponse,
    CycleDetailModel,
    CycleDetailResponse,
    CycleEvidenceResponse,
    CycleRequest,
    CycleResponse,
    CycleSourceModel,
    DoctrinesResponse,
    DreamRunEventsResponse,
    DreamRunsResponse,
    DreamRunSummaryModel,
    ErrorResponse,
    EvaluationPreviewRequest,
    EvaluationPreviewResponse,
    EventModel,
    EventsResponse,
    FactionsResponse,
    FingerprintResponse,
    HealthCheckModel,
    HealthResponse,
    IngestManifestEntryModel,
    IngestManifestRequest,
    IngestManifestResponse,
    LineageResponse,
    ManifestEntryModel,
    ManifestResponse,
    MetricsResponse,
    MultiCycleRequest,
    MultiCycleResponse,
    ProposalActionResponse,
    ProposalsResponse,
    ProposalSummaryModel,
    ProvenanceEdgeModel,
    ProvenanceResponse,
    RegistryEntriesResponse,
    RegistryEntryResponse,
    RetrievalPlanPreviewRequest,
    RetrievalPlanPreviewResponse,
    RetrievalWeightsModel,
    RetrieveCandidateModel,
    RetrieveRequest,
    RetrieveResponse,
    RunSchoolsRequest,
    RunSchoolsResponse,
    RunSchoolsResultModel,
    SchoolDistanceModel,
    SchoolDistancesResponse,
    SchoolMemberModel,
    SchoolMembersResponse,
    SchoolModel,
    SchoolRunEntryModel,
    SchoolsAllRunsResponse,
    SchoolsResponse,
    SchoolWeatherModel,
    ScoreComponentModel,
    SemanticNeighborhoodResponse,
    SemanticNeighborModel,
    SemanticNodeModel,
    SemanticTopNodesResponse,
    SourceDetailModel,
    SourceDetailResponse,
    SourceModel,
    SourcesResponse,
    StatusResponse,
    StrategyAvailabilityModel,
    SystemInfoResponse,
    WeatherResponse,
    WhyChangedResponse,
    WhyChangedTransitionModel,
)
from .db import init_db
from .diagnostics import compute_capabilities, compute_health
from .engine import FieldHorizonEngine
from .events import ACTOR_SERVER, DomainEvent, new_correlation_id
from .godot_export import build_beliefs, build_contradictions, build_doctrines, build_factions
from .lineage import LineageNotFoundError
from .manifest import ManifestError
from .proposals import ProposalError
from .temporal import TemporalNotFoundError

logger = logging.getLogger(__name__)

# Hardcoded, never configurable: this is a closed local instrument serving
# only the user's own local consumers, not a network service.
HOST = "127.0.0.1"

_LOCALHOST_ORIGIN_REGEX = r"http://(localhost|127\.0\.0\.1)(:\d+)?"

# Implementation Brief IV, Phase UI-3 item 2: the web target is `vite build`
# output from apps/desktop, served by this same server at /ui -- not a second
# app, not a separate origin. Same-origin means the browser build never needs
# a CORS grant of its own; the regex above already exists for the *dev*
# server case (npm run dev on a different port).
UI_DIST_DIR = Path(__file__).resolve().parent.parent / "apps" / "desktop" / "dist"


class _SPAStaticFiles(StaticFiles):
    """
    A client-side router needs every deep link (e.g. /ui/canon) to serve
    index.html on direct load/refresh, not 404 -- this is the standard
    Starlette pattern for that: fall back to index.html only on a genuine
    404, so a missing asset (e.g. a typo'd .js path) still surfaces as a
    real 404 instead of silently returning HTML.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def load_or_create_token(token_file: Path) -> str:
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(0o600)
    return token


class EventLedger:
    """JSONL log of every server request and engine operation, under logs/events/."""

    def __init__(self, cfg: AppConfig):
        self.path = cfg.logs / "events" / f"events_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()

    def record(self, event: str, detail: dict) -> None:
        row = {"timestamp": datetime.now().isoformat(), "event": event, **detail}
        with self._write_lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class ServerMetrics:
    def __init__(self, history: int = 500):
        self.server_requests_total = 0
        self._retrieval_latencies_ms: deque[float] = deque(maxlen=history)
        self._lock = threading.Lock()

    def record_request(self) -> None:
        with self._lock:
            self.server_requests_total += 1

    def record_retrieval_latency(self, ms: float) -> None:
        with self._lock:
            self._retrieval_latencies_ms.append(ms)

    def percentile(self, p: float) -> float | None:
        with self._lock:
            values = sorted(self._retrieval_latencies_ms)
        if not values:
            return None
        idx = min(len(values) - 1, int(p * len(values)))
        return values[idx]


class FieldHorizonAPIError(HTTPException):
    """
    Raise this instead of a plain HTTPException wherever a specific error
    code and remediation are known (FABLE Sec.13's structured error model).
    Plain HTTPException still works everywhere else -- both are converted to
    the same ErrorResponse shape by the handlers registered in create_app.
    """

    def __init__(self, status_code: int, code: str, title: str, detail: str, remediation: str):
        super().__init__(status_code=status_code, detail=detail)
        self.code = code
        self.title = title
        self.remediation = remediation


# Poll interval for the /events/stream projection below -- domain_events has
# no native push mechanism, so this is a cursor-based poll (since_id, exact
# because domain_events.id is monotonic and gap-free on this append-only
# table), not a new event bus. Kept short so a freshly written event surfaces
# quickly without hammering the database between polls.
_SSE_POLL_INTERVAL_SECONDS = 0.5


def _sse_envelope(event: DomainEvent) -> dict:
    """
    Maps the existing DomainEvent onto FABLE Sec.8.3's minimum envelope shape.
    "stage" and "severity" are always null: no such concepts exist anywhere
    in the domain-event fabric (Implementation Brief III) -- inventing values
    for them would be exactly the kind of fake facade Sec.3.2 forbids. Everything
    else the envelope calls for maps onto a real, already-recorded field.
    """
    return {
        "event_id": event.event_id,
        "run_id": event.run_id,
        "cycle_id": event.aggregate_id if event.aggregate_type == "cycle" else None,
        "timestamp": event.occurred_at,
        "event_type": event.event_type,
        "stage": None,
        "severity": None,
        "payload": event.payload,
    }


def _score_components_model(components: dict) -> dict[str, ScoreComponentModel]:
    return {name: ScoreComponentModel(raw=c.raw, weight=c.weight, contribution=c.contribution) for name, c in components.items()}


def _retrieve_candidate_model(c) -> RetrieveCandidateModel:
    """Handles both HybridCandidate (v2 -- no exclusion_reason attribute at all) and retrieval_plan.ChunkCandidate (v3)."""
    return RetrieveCandidateModel(
        chunk_id=c.chunk_id,
        canonical_ref=c.canonical_ref,
        content=c.content,
        source_title=c.source_title,
        source_type=c.source_type,
        score=c.score,
        components=_score_components_model(c.components),
        exclusion_reason=getattr(c, "exclusion_reason", None),
    )


def _canon_candidate_model(c) -> CanonCandidateModel:
    return CanonCandidateModel(
        cycle_id=c.cycle_id,
        query=c.query,
        fragment=c.fragment,
        score=c.score,
        components=_score_components_model(c.components),
        supporting_evidence_count=c.supporting_evidence_count,
        synthetic_dependency_ratio=c.synthetic_dependency_ratio,
        exclusion_reason=c.exclusion_reason,
    )


def _weather_response(engine: FieldHorizonEngine) -> WeatherResponse:
    snapshot = engine.weather()
    by_school = [
        SchoolWeatherModel(school_id=s.school_id, name=s.name, readings=s.readings) for s in engine.weather_by_school()
    ]
    return WeatherResponse(
        canon=snapshot.canon,
        surface=snapshot.surface,
        surface_history=snapshot.surface_history,
        canon_history=snapshot.canon_history,
        by_school=by_school,
    )


def _error_response(status_code: int, code: str, title: str, detail: str, remediation: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            code=code, title=title, detail=detail, remediation=remediation, correlation_id=new_correlation_id()
        ).model_dump(),
    )


def create_app(cfg: AppConfig, token: str) -> FastAPI:
    """
    Local-only platform API (Civilization Engine phase): read-mostly,
    binds to 127.0.0.1 only (enforced by the caller, run_server, which
    hardcodes HOST -- never a request parameter), bearer-token authenticated
    on every route, no outbound HTTP anywhere in this module, no webhooks.
    POST /cycle is the sole mutating endpoint and is limited to one
    concurrent execution by a plain lock.
    """
    init_db(cfg.database)
    engine = FieldHorizonEngine(cfg)
    ledger = EventLedger(cfg)
    metrics = ServerMetrics()
    cycle_lock = threading.Lock()
    ingest_lock = threading.Lock()
    multi_cycle_lock = threading.Lock()
    schools_lock = threading.Lock()

    security = HTTPBearer()

    def require_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> None:  # noqa: B008
        if not secrets.compare_digest(credentials.credentials, token):
            raise HTTPException(status_code=401, detail="Invalid or missing bearer token")

    app = FastAPI(title="Field Horizon Platform API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_LOCALHOST_ORIGIN_REGEX,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(FieldHorizonAPIError)
    async def handle_fh_error(request: Request, exc: FieldHorizonAPIError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.title, str(exc.detail), exc.remediation)

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        # Covers require_token's 401 and any plain HTTPException raised
        # anywhere in this module without a FieldHorizonAPIError's extra fields.
        return _error_response(
            exc.status_code, f"FH_HTTP_{exc.status_code}", "Request failed", str(exc.detail),
            "See detail for what to do next.",
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def handle_db_locked(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        # Phase UI-6 item 4 (Resilience): "database is locked"/"database is
        # busy" are real, recoverable cases for a single-machine app -- a CLI
        # ingest/dream run holding a write transaction while the server also
        # touches the DB -- distinct from the generic 500 below.
        # busy_timeout=5000 (db.py's connect()) already makes this rare; this
        # handler is for when a write still outlasts that window. Any other
        # OperationalError (malformed SQL, missing table) is a real bug, not
        # a transient condition, so it falls through to the same generic 500
        # the unregistered-exception handler below returns -- "retry in a
        # moment" would be actively wrong advice for those.
        message = str(exc)
        if "locked" in message or "busy" in message:
            logger.warning("Database busy on %s %s: %s", request.method, request.url.path, exc)
            return _error_response(
                503, "FH_DATABASE_BUSY", "Database busy", message,
                "Another operation (an ingest or dream run, most likely) is writing to the database. Retry in a moment.",
            )
        logger.exception("Unhandled database error on %s %s", request.method, request.url.path)
        return _error_response(
            500, "FH_INTERNAL_ERROR", "Internal error", "An unexpected error occurred.",
            "Check server logs for the full traceback; this is likely a bug worth reporting.",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # No raw traceback ever reaches the client (FABLE Sec.13) -- the real
        # exception is logged server-side, in full, for anyone reading logs.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return _error_response(
            500, "FH_INTERNAL_ERROR", "Internal error", "An unexpected error occurred.",
            "Check server logs for the full traceback; this is likely a bug worth reporting.",
        )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - start) * 1000
        metrics.record_request()
        ledger.record(
            "server_request",
            {
                "path": request.url.path,
                "method": request.method,
                "status": response.status_code,
                "elapsed_ms": round(elapsed_ms, 2),
            },
        )
        return response

    @app.get("/status", response_model=StatusResponse, dependencies=[Depends(require_token)])
    def get_status() -> StatusResponse:
        stats = engine.stats()
        return StatusResponse(**stats.__dict__)

    @app.get("/system-info", response_model=SystemInfoResponse, dependencies=[Depends(require_token)])
    def get_system_info() -> SystemInfoResponse:
        return SystemInfoResponse(
            version=fieldhorizon.__version__,
            default_model=cfg.default_model,
            embedding_model=cfg.embedding_model,
            ollama_base_url=cfg.ollama_base_url,
            mode=cfg.mode,
            tone=cfg.tone,
            database_path=str(cfg.database),
            default_weights=RetrievalWeightsModel(
                vector_similarity=cfg.retrieval_weights.vector_similarity,
                bm25=cfg.retrieval_weights.bm25,
                domain_prior=cfg.retrieval_weights.domain_prior,
                source_weight=cfg.retrieval_weights.source_weight,
                severity=cfg.retrieval_weights.severity,
            ),
        )

    @app.get("/config", response_model=ConfigResponse, dependencies=[Depends(require_token)])
    def get_config() -> ConfigResponse:
        """Raw config.yaml, re-read from disk -- no secrets live in this file (audited, Phase UI-1). Used by the reproducible cycle bundle export (Phase UI-6 item 6) and available for the same read-only SETTINGS comparison engine.config() was already built for."""
        return ConfigResponse(config=engine.config())

    @app.get("/canon", response_model=CanonResponse, dependencies=[Depends(require_token)])
    def get_canon(
        verdict: str = "CANON",
        include_retired: bool = False,
        limit: int = 50,
        as_of_cycle: int | None = None,
        as_of_timestamp: str | None = None,
    ) -> CanonResponse:
        """
        as_of_cycle/as_of_timestamp (Implementation Brief III, Phase D) each
        reconstruct active CANON as of a point in time -- verdict/include_retired/
        limit are ignored in that mode, matching engine.canon_as_of_cycle/timestamp's
        own fixed shape (they always return active CANON, nothing else).
        """
        if as_of_cycle is not None:
            try:
                entries = engine.canon_as_of_cycle(as_of_cycle)
            except TemporalNotFoundError as exc:
                raise FieldHorizonAPIError(
                    404, "FH_CYCLE_NOT_FOUND", "Cycle not found", str(exc), "Check the cycle id and try again."
                ) from exc
        elif as_of_timestamp is not None:
            entries = engine.canon_as_of_timestamp(as_of_timestamp)
        else:
            entries = engine.canon(verdict=verdict, include_retired=include_retired, limit=limit)
        return CanonResponse(entries=[CanonEntryModel(**e.__dict__) for e in entries])

    @app.get("/why-changed/{cycle_id}", response_model=WhyChangedResponse, dependencies=[Depends(require_token)])
    def get_why_changed(cycle_id: int) -> WhyChangedResponse:
        result = engine.why_changed(cycle_id)
        return WhyChangedResponse(
            cycle_id=result.cycle_id,
            transitions=[
                WhyChangedTransitionModel(
                    from_verdict=t.from_state.verdict if t.from_state is not None else None,
                    to_verdict=t.to_state.verdict,
                    valid_from=t.to_state.valid_from,
                    active=t.to_state.active,
                    event_type=t.event.event_type if t.event is not None else None,
                    edge_relation_type=t.edge.relation_type if t.edge is not None else None,
                )
                for t in result.transitions
            ],
        )

    @app.get("/lineage/{target_id}", response_model=LineageResponse, dependencies=[Depends(require_token)])
    def get_lineage(target_id: str) -> LineageResponse:
        try:
            result = engine.lineage(target_id)
        except LineageNotFoundError as exc:
            raise FieldHorizonAPIError(
                404, "FH_LINEAGE_TARGET_NOT_FOUND", "Lineage target not found", str(exc),
                "Use a numeric cycle id for canon genealogy, or a json_entries axiom id for pressure-edge provenance.",
            ) from exc
        return LineageResponse(target_id=result.target_id, text=result.text)

    @app.get("/provenance/{target_id}", response_model=ProvenanceResponse, dependencies=[Depends(require_token)])
    def get_provenance(target_id: str) -> ProvenanceResponse:
        try:
            report = engine.provenance(target_id)
        except LineageNotFoundError as exc:
            raise FieldHorizonAPIError(
                404, "FH_LINEAGE_TARGET_NOT_FOUND", "Lineage target not found", str(exc),
                "Use a numeric cycle id for canon genealogy, or a json_entries axiom id for pressure-edge provenance.",
            ) from exc
        weakest_link = (
            ProvenanceEdgeModel(
                source_type=report.weakest_link.source_type,
                source_id=report.weakest_link.source_id,
                target_type=report.weakest_link.target_type,
                target_id=report.weakest_link.target_id,
                relation_type=report.weakest_link.relation_type,
                confidence=report.weakest_link.confidence,
            )
            if report.weakest_link is not None
            else None
        )
        return ProvenanceResponse(
            object_type=report.object_type,
            object_id=report.object_id,
            supporting_evidence_count=report.supporting_evidence_count,
            opposing_evidence_count=report.opposing_evidence_count,
            circular_ancestry=report.circular_ancestry,
            synthetic_dependency_ratio=report.synthetic_dependency_ratio,
            completeness_score=report.completeness_score,
            missing_links=report.missing_links,
            weakest_link=weakest_link,
            model_registry_ids=report.model_registry_ids,
        )

    @app.get("/weather", response_model=WeatherResponse, dependencies=[Depends(require_token)])
    def get_weather(as_of: str | None = None) -> WeatherResponse:
        if as_of is not None:
            readings = engine.weather_as_of(as_of)
            return WeatherResponse(canon=readings, surface={}, surface_history={})
        return _weather_response(engine)

    @app.get("/schools", response_model=SchoolsResponse, dependencies=[Depends(require_token)])
    def get_schools(as_of: str | None = None) -> SchoolsResponse:
        schools = engine.school_membership_as_of(as_of) if as_of is not None else engine.schools()
        return SchoolsResponse(schools=[SchoolModel(**s.__dict__) for s in schools])

    @app.get("/schools/all", response_model=SchoolsAllRunsResponse, dependencies=[Depends(require_token)])
    def get_schools_all_runs() -> SchoolsAllRunsResponse:
        return SchoolsAllRunsResponse(schools=[SchoolRunEntryModel(**s.__dict__) for s in engine.schools_all_runs()])

    @app.get("/schools/{school_id}/members", response_model=SchoolMembersResponse, dependencies=[Depends(require_token)])
    def get_school_members(school_id: int) -> SchoolMembersResponse:
        members = engine.school_members(school_id)
        return SchoolMembersResponse(school_id=school_id, members=[SchoolMemberModel(**m.__dict__) for m in members])

    @app.get("/schools/distances", response_model=SchoolDistancesResponse, dependencies=[Depends(require_token)])
    def get_school_distances(run_at: str) -> SchoolDistancesResponse:
        distances = engine.school_distances(run_at)
        return SchoolDistancesResponse(run_at=run_at, distances=[SchoolDistanceModel(**d.__dict__) for d in distances])

    @app.post("/schools/run", response_model=RunSchoolsResponse, dependencies=[Depends(require_token)])
    def post_run_schools(body: RunSchoolsRequest) -> RunSchoolsResponse:
        if not schools_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=429, detail="A schools clustering run is already in progress; only one concurrent run is allowed."
            )
        correlation_id = body.correlation_id or new_correlation_id()
        try:
            results = engine.run_schools_clustering(
                k=body.k, seed=body.seed, model=body.model, actor=ACTOR_SERVER, correlation_id=correlation_id
            )
        finally:
            schools_lock.release()
        return RunSchoolsResponse(
            correlation_id=correlation_id,
            schools=[
                RunSchoolsResultModel(
                    school_id=r.school_id,
                    name=r.name,
                    summary=r.summary,
                    member_cycle_ids=r.member_cycle_ids,
                    previous_school_id=r.previous_school_id,
                )
                for r in results
            ],
        )

    @app.get("/councils", response_model=CouncilsResponse, dependencies=[Depends(require_token)])
    def get_councils(limit: int = 20) -> CouncilsResponse:
        return CouncilsResponse(councils=[CouncilModel(**c.__dict__) for c in engine.councils(limit=limit)])

    @app.get("/councils/{council_id}", response_model=CouncilDetailResponse, dependencies=[Depends(require_token)])
    def get_council_detail(council_id: int) -> CouncilDetailResponse:
        detail = engine.council_detail(council_id)
        return CouncilDetailResponse(
            council_id=detail.council_id,
            rehabilitated=[CouncilCaseModel(**c.__dict__) for c in detail.rehabilitated],
            retired=[CouncilCaseModel(**c.__dict__) for c in detail.retired],
            promoted=[CouncilCaseModel(**c.__dict__) for c in detail.promoted],
        )

    @app.get("/sources", response_model=SourcesResponse, dependencies=[Depends(require_token)])
    def get_sources(limit: int = 200, offset: int = 0, q: str | None = None) -> SourcesResponse:
        return SourcesResponse(
            sources=[
                SourceModel(
                    id=s.id, title=s.title, source_type=s.source_type, manifest_id=s.manifest_id, weight=s.weight
                )
                for s in engine.sources(limit=limit, offset=offset, q=q)
            ],
            total=engine.sources_count(q=q),
        )

    @app.get("/sources/{source_id}", response_model=SourceDetailResponse, dependencies=[Depends(require_token)])
    def get_source_detail(source_id: int) -> SourceDetailResponse:
        source = engine.source(source_id)
        if source is None:
            raise FieldHorizonAPIError(
                404, "FH_SOURCE_NOT_FOUND", "Source not found", f"No source with id {source_id}.",
                "Check GET /sources for valid ids.",
            )
        return SourceDetailResponse(source=SourceDetailModel(**source.__dict__))

    @app.get("/sources/{source_id}/chunks", response_model=ChunksResponse, dependencies=[Depends(require_token)])
    def get_source_chunks(source_id: int, limit: int = 200) -> ChunksResponse:
        chunks = engine.source_chunks(source_id, limit=limit)
        return ChunksResponse(chunks=[ChunkModel(**c.__dict__) for c in chunks])

    @app.get("/cycles/{cycle_id}", response_model=CycleDetailResponse, dependencies=[Depends(require_token)])
    def get_cycle_detail(cycle_id: int) -> CycleDetailResponse:
        """Phase UI-6 item 6: the reproducible cycle bundle needed a single-cycle lookup that didn't exist before -- /canon only ever filters by verdict, never by cycle_id."""
        cycle = engine.cycle_detail(cycle_id)
        if cycle is None:
            raise FieldHorizonAPIError(
                404, "FH_CYCLE_NOT_FOUND", "Cycle not found", f"No cycle with id {cycle_id}.",
                "Check GET /canon or GET /events for valid cycle ids.",
            )
        return CycleDetailResponse(cycle=CycleDetailModel(**cycle.__dict__))

    @app.get("/cycles/{cycle_id}/evidence", response_model=CycleEvidenceResponse, dependencies=[Depends(require_token)])
    def get_cycle_evidence(cycle_id: int) -> CycleEvidenceResponse:
        """The verbatim evidence text recorded in cycle_sources at cycle time -- the same table replay.py's replay_cycle_level3 reads for exact reproduction, now reachable from the API too."""
        return CycleEvidenceResponse(evidence=[CycleSourceModel(**e.__dict__) for e in engine.cycle_evidence(cycle_id)])

    @app.get("/registry/{kind}", response_model=RegistryEntriesResponse, dependencies=[Depends(require_token)])
    def get_registry_list(kind: str) -> RegistryEntriesResponse:
        return RegistryEntriesResponse(entries=engine.registry_list(kind))

    @app.get(
        "/registry/{kind}/{entry_id}", response_model=RegistryEntryResponse, dependencies=[Depends(require_token)]
    )
    def get_registry_entry(kind: str, entry_id: str) -> RegistryEntryResponse:
        entry = engine.registry_entry(kind, entry_id)
        if entry is None:
            raise FieldHorizonAPIError(
                404, "FH_REGISTRY_ENTRY_NOT_FOUND", "Registry entry not found", f"No {kind} entry {entry_id!r}.",
                f"Check GET /registry/{kind} for valid ids.",
            )
        return RegistryEntryResponse(entry=entry)

    @app.get("/events", response_model=EventsResponse, dependencies=[Depends(require_token)])
    def get_events(
        run_id: str | None = None,
        cycle_id: int | None = None,
        event_type: str | None = None,
        since_id: int | None = None,
        limit: int = 200,
    ) -> EventsResponse:
        events = engine.events(run_id=run_id, cycle_id=cycle_id, event_type=event_type, since_id=since_id, limit=limit)
        return EventsResponse(events=[EventModel(**e.__dict__) for e in events])

    @app.get("/events/stream", dependencies=[Depends(require_token)])
    async def get_events_stream(
        request: Request,
        run_id: str | None = None,
        cycle_id: int | None = None,
        event_type: str | None = None,
        since_id: int | None = None,
    ) -> StreamingResponse:
        async def event_source():
            cursor = since_id
            while True:
                events = engine.events(
                    run_id=run_id, cycle_id=cycle_id, event_type=event_type, since_id=cursor, limit=200
                )
                for event in events:
                    cursor = event.id
                    yield f"id: {event.id}\ndata: {json.dumps(_sse_envelope(event))}\n\n"
                # Checked after flushing whatever is already available, not before --
                # a client that disconnected mid-poll still gets the events already
                # fetched instead of losing them to a check ordered ahead of the yield.
                if await request.is_disconnected():
                    break
                await asyncio.sleep(_SSE_POLL_INTERVAL_SECONDS)

        return StreamingResponse(event_source(), media_type="text/event-stream")

    @app.get("/events/{event_id}", response_model=EventModel, dependencies=[Depends(require_token)])
    def get_event(event_id: str) -> EventModel:
        event = engine.event(event_id)
        if event is None:
            raise FieldHorizonAPIError(
                404, "FH_EVENT_NOT_FOUND", "Event not found", f"No event {event_id!r}.",
                "Check GET /events for valid ids.",
            )
        return EventModel(**event.__dict__)

    @app.get("/proposals", response_model=ProposalsResponse, dependencies=[Depends(require_token)])
    def get_proposals() -> ProposalsResponse:
        return ProposalsResponse(
            proposals=[
                ProposalSummaryModel(number=p.number, name=p.name, motif=p.motif, count=p.count)
                for p in engine.proposals()
            ]
        )

    @app.get("/proposals/{number}", dependencies=[Depends(require_token)])
    def get_proposal(number: int) -> dict:
        try:
            return engine.proposal(number)
        except ProposalError as exc:
            raise FieldHorizonAPIError(
                404, "FH_PROPOSAL_NOT_FOUND", "Proposal not found", str(exc), "Check GET /proposals for valid numbers."
            ) from exc

    @app.post("/proposals/{number}/apply", response_model=ProposalActionResponse, dependencies=[Depends(require_token)])
    def post_apply_proposal(number: int) -> ProposalActionResponse:
        """
        Merges the proposal into ontology.yaml, parity-checks, and
        git-commits (proposals.apply_proposal) -- never call this without
        the frontend's own explicit confirmation step (FABLE Sec.10.6:
        "apply requires explicit confirmation and shows the parity-check
        result. Never auto-apply."). A parity failure's full pytest output
        is preserved in the error detail, not summarized away.
        """
        try:
            engine.proposal(number)
        except ProposalError as exc:
            raise FieldHorizonAPIError(
                404, "FH_PROPOSAL_NOT_FOUND", "Proposal not found", str(exc), "Check GET /proposals for valid numbers."
            ) from exc
        try:
            path = engine.proposal_apply(number, actor=ACTOR_SERVER)
        except ProposalError as exc:
            raise FieldHorizonAPIError(
                409, "FH_PROPOSAL_APPLY_FAILED", "Proposal apply failed", str(exc),
                "Fix the underlying issue (domain name collision or ontology parity failure) and retry.",
            ) from exc
        return ProposalActionResponse(number=number, path=str(path))

    @app.post("/proposals/{number}/reject", response_model=ProposalActionResponse, dependencies=[Depends(require_token)])
    def post_reject_proposal(number: int) -> ProposalActionResponse:
        try:
            path = engine.proposal_reject(number, actor=ACTOR_SERVER)
        except ProposalError as exc:
            raise FieldHorizonAPIError(
                404, "FH_PROPOSAL_NOT_FOUND", "Proposal not found", str(exc), "Check GET /proposals for valid numbers."
            ) from exc
        return ProposalActionResponse(number=number, path=str(path))

    @app.get("/dreams", response_model=DreamRunsResponse, dependencies=[Depends(require_token)])
    def get_dreams() -> DreamRunsResponse:
        return DreamRunsResponse(runs=[DreamRunSummaryModel(**r.__dict__) for r in engine.dream_runs()])

    @app.get("/dreams/{stamp}", response_model=DreamRunEventsResponse, dependencies=[Depends(require_token)])
    def get_dream_run(stamp: str) -> DreamRunEventsResponse:
        events = engine.dream_run_events(stamp)
        if events is None:
            raise FieldHorizonAPIError(
                404, "FH_DREAM_RUN_NOT_FOUND", "Dream run not found", f"No dream run ledger for {stamp!r}",
                "Check GET /dreams for valid stamps.",
            )
        return DreamRunEventsResponse(stamp=stamp, events=events)

    def _validated_semantic_kind(kind: str) -> str:
        if kind not in SEMANTIC_KINDS:
            raise FieldHorizonAPIError(
                400, "FH_INVALID_SEMANTIC_KIND", "Invalid semantic kind", f"{kind!r} is not domain/entity/motif",
                "Use one of: domain, entity, motif.",
            )
        return kind

    @app.get("/semantics/top", response_model=SemanticTopNodesResponse, dependencies=[Depends(require_token)])
    def get_semantic_top(kind: str, limit: int = 20) -> SemanticTopNodesResponse:
        kind = _validated_semantic_kind(kind)
        return SemanticTopNodesResponse(
            kind=kind, nodes=[SemanticNodeModel(**n.__dict__) for n in engine.semantic_top_nodes(kind, limit=limit)]
        )

    @app.get("/semantics/neighbors", response_model=SemanticNeighborhoodResponse, dependencies=[Depends(require_token)])
    def get_semantic_neighbors(kind: str, label: str, limit: int = 15) -> SemanticNeighborhoodResponse:
        kind = _validated_semantic_kind(kind)
        result = engine.semantic_neighbors(kind, label, limit=limit)
        return SemanticNeighborhoodResponse(
            kind=result.kind,
            label=result.label,
            neighbors=[SemanticNeighborModel(**n.__dict__) for n in result.neighbors],
            total_neighbor_count=result.total_neighbor_count,
        )

    @app.get("/capabilities", response_model=CapabilitiesResponse, dependencies=[Depends(require_token)])
    def get_capabilities() -> CapabilitiesResponse:
        capabilities = compute_capabilities(cfg)
        return CapabilitiesResponse(
            capabilities={
                name: CapabilityModel(status=cap.status, detail=cap.detail) for name, cap in capabilities.items()
            }
        )

    @app.get("/health", response_model=HealthResponse, dependencies=[Depends(require_token)])
    def get_health() -> HealthResponse:
        checks = compute_health(cfg)
        statuses = {c.status for c in checks}
        overall = "failed" if "failed" in statuses else "degraded" if "degraded" in statuses else "ok"
        return HealthResponse(
            status=overall,
            checks=[HealthCheckModel(name=c.name, status=c.status, detail=c.detail) for c in checks],
        )

    @app.post("/retrieve", response_model=RetrieveResponse, dependencies=[Depends(require_token)])
    def post_retrieve(body: RetrieveRequest) -> RetrieveResponse:
        weights = (
            RetrievalWeights(
                vector_similarity=body.weights.vector_similarity,
                bm25=body.weights.bm25,
                domain_prior=body.weights.domain_prior,
                source_weight=body.weights.source_weight,
                severity=body.weights.severity,
            )
            if body.weights
            else None
        )

        start = time.monotonic()
        if body.plan:
            result = engine.retrieve_planned(
                body.query, limit=body.limit, as_of=body.as_of, weights=weights, actor=ACTOR_SERVER
            )
            metrics.record_retrieval_latency((time.monotonic() - start) * 1000)
            return RetrieveResponse(
                candidates=[_retrieve_candidate_model(c) for c in result.chunk_candidates],
                used_planner=True,
                canon_candidates=[_canon_candidate_model(c) for c in result.canon_candidates],
                excluded_candidates=[_retrieve_candidate_model(c) for c in result.excluded_chunk_candidates],
                excluded_canon_candidates=[_canon_candidate_model(c) for c in result.excluded_canon_candidates],
                constraints_applied=result.constraints_applied,
                strategies_selected=result.plan.strategies_selected,
                classification=result.plan.classification,
            )

        candidates = engine.retrieve(body.query, limit=body.limit, explain=body.explain, weights=weights)
        metrics.record_retrieval_latency((time.monotonic() - start) * 1000)
        return RetrieveResponse(candidates=[_retrieve_candidate_model(c) for c in candidates])

    @app.post("/planner/preview", response_model=RetrievalPlanPreviewResponse, dependencies=[Depends(require_token)])
    def post_planner_preview(body: RetrievalPlanPreviewRequest) -> RetrievalPlanPreviewResponse:
        """Classification only (no candidate search) -- which real strategies would fire for this query and why, cheaper than /retrieve?plan=true."""
        plan = engine.retrieval_plan_preview(body.query, as_of=body.as_of)
        return RetrievalPlanPreviewResponse(
            query=plan.query,
            strategies_selected=plan.strategies_selected,
            strategies_available=[
                StrategyAvailabilityModel(name=s.name, backed=s.backed, reason=s.reason) for s in plan.strategies_available
            ],
            classification=plan.classification,
        )

    @app.post("/evaluation/preview", response_model=EvaluationPreviewResponse, dependencies=[Depends(require_token)])
    def post_evaluation_preview(body: EvaluationPreviewRequest) -> EvaluationPreviewResponse:
        """Runs the real evaluator standalone on arbitrary text -- no cycle, no retrieval, no canonization (EVALUATION lab)."""
        result = engine.evaluate_fragment(body.text)
        return EvaluationPreviewResponse(**result.__dict__)

    @app.post("/cycle", response_model=CycleResponse, dependencies=[Depends(require_token)])
    def post_cycle(body: CycleRequest) -> CycleResponse:
        if not cycle_lock.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="A cycle is already running; only one concurrent cycle is allowed.")
        try:
            cycle_id = engine.cycle(
                body.query,
                model=body.model,
                dry_run=body.dry_run,
                auto_rewrite=body.auto_rewrite,
                critic_model=body.critic_model,
                rewrite_model=body.rewrite_model,
                json_domain=body.json_domain,
                actor=ACTOR_SERVER,
            )
        finally:
            cycle_lock.release()
        # No hand-rolled ledger.record here: run_cycle now emits the full
        # CycleStarted..CycleCompleted/Failed chain itself (Implementation
        # Brief III, Phase A), identically whether called from the CLI or
        # from this route -- that's the whole point of centralizing
        # emission in the facade's underlying call, not the caller.
        return CycleResponse(cycle_id=cycle_id)

    @app.post("/multi-cycle", response_model=MultiCycleResponse, dependencies=[Depends(require_token)])
    def post_multi_cycle(body: MultiCycleRequest) -> MultiCycleResponse:
        """
        The multi-agent path (four adversaries + synthesizer + evaluator +
        optional critic/rewrite) -- CycleStarted/RetrievalCompleted/
        AgentCompleted(x4)/EvaluationCompleted/CycleCompleted all real,
        watchable live via GET /events/stream?run_id=<correlation_id>
        (Phase UI-4 item 4's LIVE CYCLE). Plain /cycle above has none of
        this -- single-agent, no debate, kept exactly as it was.
        """
        if not multi_cycle_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=429, detail="A multi-cycle is already running; only one concurrent run is allowed."
            )
        correlation_id = body.correlation_id or new_correlation_id()
        try:
            cycle_id = engine.multi_cycle(
                body.query,
                model=body.model,
                agent_model=body.agent_model,
                synthesizer_model=body.synthesizer_model,
                critic_model=body.critic_model,
                rewrite_model=body.rewrite_model,
                interpreter_model=body.interpreter_model,
                dry_run=body.dry_run,
                auto_rewrite=body.auto_rewrite,
                json_domain=body.json_domain,
                actor=ACTOR_SERVER,
                correlation_id=correlation_id,
            )
        finally:
            multi_cycle_lock.release()
        return MultiCycleResponse(cycle_id=cycle_id, correlation_id=correlation_id)

    @app.post("/ingest/manifest", response_model=IngestManifestResponse, dependencies=[Depends(require_token)])
    def post_ingest_manifest(body: IngestManifestRequest) -> IngestManifestResponse:
        if not ingest_lock.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="An ingest run is already in progress; only one concurrent run is allowed.")
        # Minted here (not left to ingest_from_manifest) so it's known and
        # returned even if the caller didn't supply one -- the client
        # couldn't otherwise learn which run_id to filter
        # GET /events/stream by until this synchronous call finishes.
        correlation_id = body.correlation_id or new_correlation_id()
        try:
            results = engine.ingest_manifest(actor=ACTOR_SERVER, correlation_id=correlation_id)
        finally:
            ingest_lock.release()
        return IngestManifestResponse(
            correlation_id=correlation_id,
            entries=[
                IngestManifestEntryModel(
                    manifest_id=r.manifest_id, title=r.title, status=r.status,
                    chunk_count=r.chunk_count, source_id=r.source_id,
                )
                for r in results
            ],
        )

    @app.get("/manifest", response_model=ManifestResponse, dependencies=[Depends(require_token)])
    def get_manifest() -> ManifestResponse:
        try:
            entries = engine.manifest_entries()
        except ManifestError as exc:
            raise FieldHorizonAPIError(
                404, "FH_MANIFEST_NOT_FOUND", "Manifest not found", str(exc),
                "Create data/sources.yaml (see its own header comment for the format), then retry.",
            ) from exc
        return ManifestResponse(
            entries=[
                ManifestEntryModel(
                    id=e.id, title=e.title, path=e.path, author=e.author, year=e.year,
                    language=e.language, license=e.license, domain_hints=list(e.domain_hints),
                    weight=e.weight, notes=e.notes,
                )
                for e in entries
            ]
        )

    @app.get("/fingerprint/{source_id}", response_model=FingerprintResponse, dependencies=[Depends(require_token)])
    def get_fingerprint(source_id: int) -> FingerprintResponse:
        fp = engine.fingerprint(source_id)
        if fp is None:
            raise FieldHorizonAPIError(
                404, "FH_SOURCE_NOT_FINGERPRINTED", "Source has no fingerprint yet", "Source has no embedded chunks yet.",
                "Run chunk-embedding backfill for this source, then retry.",
            )
        return FingerprintResponse(
            source_id=fp.source_id,
            embedding_version=fp.embedding_version,
            domain_distribution=fp.domain_distribution,
            top_motifs=fp.top_motifs,
            chunk_count=fp.chunk_count,
        )

    @app.get("/export/factions", response_model=FactionsResponse, dependencies=[Depends(require_token)])
    def get_export_factions() -> FactionsResponse:
        return build_factions(engine)

    @app.get("/export/doctrines", response_model=DoctrinesResponse, dependencies=[Depends(require_token)])
    def get_export_doctrines() -> DoctrinesResponse:
        return build_doctrines(engine)

    @app.get("/export/contradictions", response_model=ContradictionsResponse, dependencies=[Depends(require_token)])
    def get_export_contradictions() -> ContradictionsResponse:
        return build_contradictions(engine)

    @app.get("/export/weather", response_model=WeatherResponse, dependencies=[Depends(require_token)])
    def get_export_weather() -> WeatherResponse:
        return _weather_response(engine)

    @app.get("/export/beliefs", response_model=BeliefsResponse, dependencies=[Depends(require_token)])
    def get_export_beliefs() -> BeliefsResponse:
        return build_beliefs(engine)

    @app.get("/metrics", response_model=MetricsResponse, dependencies=[Depends(require_token)])
    def get_metrics() -> MetricsResponse:
        stats = engine.stats()
        return MetricsResponse(
            cycles_run=stats.cycles_total,
            canon_size=stats.canon_count,
            heresy_size=stats.heresy_count,
            councils_run=stats.councils_count,
            dream_runs=stats.dream_runs_count,
            server_requests_total=metrics.server_requests_total,
            retrieval_latency_p50_ms=metrics.percentile(0.50),
            retrieval_latency_p95_ms=metrics.percentile(0.95),
        )

    if UI_DIST_DIR.is_dir():
        app.mount("/ui", _SPAStaticFiles(directory=UI_DIST_DIR, html=True), name="ui")
    else:
        logger.info("No built frontend at %s -- /ui not mounted (run `scripts/gui-build` to produce it).", UI_DIST_DIR)

    return app


def run_server(cfg: AppConfig, port: int, token_file: Path) -> None:
    import uvicorn

    token = load_or_create_token(token_file)
    app = create_app(cfg, token)
    uvicorn.run(app, host=HOST, port=port, log_level="warning")
