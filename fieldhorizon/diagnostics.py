from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from .config import AppConfig
from .db import CURRENT_SCHEMA_VERSION, connect
from .manifests import db_schema_version
from .rustcore import rust_binary_path

logger = logging.getLogger(__name__)

# Implementation Brief IV, Phase UI-2 item 2: /capabilities reports what is
# ACTUALLY present, computed by introspecting real state (a file exists, a
# table is reachable) wherever that's possible, rather than a hand-maintained
# dict that drifts from reality the moment a feature ships or regresses --
# the whole "no fake facade" guarantee (FABLE Sec.3.2) depends on this endpoint
# staying honest automatically. Statuses mirror docs/gui/REPOSITORY_REALITY_AUDIT.md
# exactly; update both together if a capability's real status ever changes.

STATUS_PRESENT = "present"
STATUS_PARTIAL = "partial"
STATUS_ABSENT = "absent"


@dataclass(frozen=True)
class Capability:
    status: str
    detail: str = ""


def compute_capabilities(cfg: AppConfig) -> dict[str, Capability]:
    rust_present = rust_binary_path(cfg).exists()

    return {
        "retrieval_planner": Capability(
            STATUS_PRESENT, "Rule-based classifier, 8 backed strategies (Implementation Brief III, Phase E)."
        ),
        "planner_modes": Capability(
            STATUS_ABSENT, "No legacy/shadow/active modes exist -- one opt-in --plan flag on retrieval."
        ),
        "contradictions": Capability(
            STATUS_PARTIAL,
            "Ontological pressure edges only (axiom vs axiom, godot_export.build_contradictions), "
            "computed live and never persisted; no severity typology; no CONTRADICTS writer anywhere.",
        ),
        "provenance_graph": Capability(STATUS_PRESENT, "11 relation types, backfillable (Brief III Phase C)."),
        "bitemporal_canon": Capability(STATUS_PRESENT, "valid-time and transaction-time as-of queries (Brief III Phase D)."),
        "domain_events": Capability(STATUS_PRESENT, "Full event fabric merged; every cycle/council/dream/retrieval-plan operation emits."),
        "tiered_replay": Capability(STATUS_PRESENT, "Levels 1-6."),
        "registries": Capability(STATUS_PRESENT, "Model/prompt/embedding, auto-register-on-first-sight."),
        "schools": Capability(STATUS_PRESENT),
        "weather": Capability(STATUS_PRESENT),
        "councils": Capability(STATUS_PRESENT),
        "dreams": Capability(STATUS_PRESENT, "Budget-capped exploration + ontology proposal review."),
        "principal_context": Capability(STATUS_PRESENT, "Internal parameter seam only; not exposed via API or CLI flag."),
        "postgres": Capability(STATUS_ABSENT, "SQLite only; no abstraction layer exists."),
        "rust_pressure_core": Capability(STATUS_PRESENT if rust_present else STATUS_ABSENT),
        "pdf_ingestion": Capability(STATUS_ABSENT, "No PDF parser anywhere in the tree."),
    }


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: str  # "ok" | "degraded" | "failed"
    detail: str = ""


def _check_db(cfg: AppConfig) -> HealthCheck:
    try:
        with connect(cfg.database) as conn:
            conn.execute("SELECT 1").fetchone()
        return HealthCheck(name="db", status="ok")
    except Exception as exc:
        return HealthCheck(name="db", status="failed", detail=str(exc))


def _check_schema(cfg: AppConfig) -> HealthCheck:
    try:
        version = db_schema_version(cfg)
    except Exception as exc:
        return HealthCheck(name="schema", status="failed", detail=str(exc))
    if version < CURRENT_SCHEMA_VERSION:
        return HealthCheck(
            name="schema", status="degraded",
            detail=f"database at schema version {version}, code expects {CURRENT_SCHEMA_VERSION} -- run init_db",
        )
    return HealthCheck(name="schema", status="ok", detail=f"version {version}")


def _check_model_provider(cfg: AppConfig) -> HealthCheck:
    """
    Best-effort, matching this codebase's own long-standing treatment of
    Ollama as a degradable dependency (see registries._resolve_model_digest's
    own fallback) -- an unreachable model provider is 'degraded', never
    'failed': the rest of the system (corpus browsing, canon, provenance,
    replay of already-recorded cycles) works fine without it.

    Phase UI-6 item 4 (Resilience): reachability alone used to be the whole
    check -- "Ollama process down" was visible, but "Ollama running, correct
    model never pulled" (a real, easy-to-hit setup mistake) was not. Now
    parses the tags response and checks cfg.default_model against it too.
    """
    try:
        response = requests.get(f"{cfg.ollama_base_url}/api/tags", timeout=2)
        response.raise_for_status()
        pulled = {entry.get("name") for entry in response.json().get("models", [])}
        if cfg.default_model not in pulled:
            return HealthCheck(
                name="model_provider",
                status="degraded",
                detail=f"Ollama reachable but '{cfg.default_model}' is not pulled -- run `ollama pull {cfg.default_model}`.",
            )
        return HealthCheck(name="model_provider", status="ok")
    except Exception as exc:
        return HealthCheck(name="model_provider", status="degraded", detail=f"Ollama unreachable: {exc}")


def _check_event_channel(cfg: AppConfig) -> HealthCheck:
    try:
        with connect(cfg.database) as conn:
            conn.execute("SELECT COUNT(*) FROM domain_events").fetchone()
        return HealthCheck(name="event_channel", status="ok")
    except Exception as exc:
        return HealthCheck(name="event_channel", status="failed", detail=str(exc))


def compute_health(cfg: AppConfig) -> list[HealthCheck]:
    return [_check_db(cfg), _check_schema(cfg), _check_model_provider(cfg), _check_event_channel(cfg)]
