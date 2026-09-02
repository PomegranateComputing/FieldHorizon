"""
Harvester configuration: `config/corpus_sources.yaml` and
`config/corpus_policy.yaml`.

Split into two files on purpose. Sources change often (add a library,
retune a query, disable one that broke); policy changes rarely and is
the file to review when asking "what is this system allowed to do". A
single merged file makes the second question much harder to answer.

Paths resolve relative to the Field Horizon root (`AppConfig.root`),
matching how `config.yaml` and `ontology.yaml` already behave -- so
running the CLI from anywhere still finds the right files.

Three validation rules are enforced here rather than left to fail later:

* A source with no `allowed_hosts` is a configuration error, not a
  permissive default. An adapter with no allowlist can fetch anywhere.
* A source that is `enabled` but names an unknown adapter is an error.
* A storage budget of zero, or a missing `min_free_disk_bytes`, makes
  autonomous modes refuse to start (see `scheduler.check_budget`).

The harvester contact address is **never** in a config file. It comes
from `$FIELDHORIZON_HARVESTER_CONTACT`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import AppConfig
from .adapters.base import SourceConfig
from .classification import DEFAULT_MANIFESTO_THRESHOLD
from .quality import QualityThresholds
from .selection import SelectionWeights

logger = logging.getLogger(__name__)

CONTACT_ENV_VAR = "FIELDHORIZON_HARVESTER_CONTACT"

DEFAULT_SOURCES_PATH = "config/corpus_sources.yaml"
DEFAULT_POLICY_PATH = "config/corpus_policy.yaml"


class PolicyError(ValueError):
    """The harvester configuration is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class Budget:
    """
    Every bound the brief names, in one place.

    All are hard limits checked before a download, not advisory targets.
    `max_total_corpus_bytes` and `min_free_disk_bytes` are the two that
    autonomous mode refuses to run without.
    """

    max_items_per_run: int = 50
    max_download_bytes_per_run: int = 1024 * 1024 * 1024
    max_total_corpus_bytes: int = 0
    min_free_disk_bytes: int = 0
    max_concurrency: int = 2
    per_source_concurrency: int = 1
    requests_per_minute: float = 20.0
    max_retries: int = 3
    request_timeout: float = 30.0
    decompressed_size_limit: int = 512 * 1024 * 1024
    max_candidates_per_source: int = 500
    max_file_bytes: int = 64 * 1024 * 1024

    @classmethod
    def from_dict(cls, data: dict | None) -> Budget:
        data = data or {}
        known = set(cls.__dataclass_fields__)
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                logger.warning("Unknown budget key %r in corpus_policy.yaml; ignoring", key)
                continue
            field_type = cls.__dataclass_fields__[key].type
            kwargs[key] = float(value) if "float" in str(field_type) else int(value)
        return cls(**kwargs)

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class ExportPolicy:
    """
    Which scopes an export profile may package. The default forbids
    LOCAL_US_ONLY material in a worldwide release -- the export guard.
    """

    allow_local_us_in_worldwide: bool = False
    require_attribution_file: bool = True
    include_quarantined: bool = False


@dataclass
class HarvesterPolicy:
    """The resolved contents of corpus_policy.yaml."""

    profile: str = "broad"
    rights_profile: str = "local_research_us"
    budget: Budget = field(default_factory=Budget)
    selection_weights: SelectionWeights = field(default_factory=SelectionWeights)
    quality: QualityThresholds = field(default_factory=QualityThresholds)
    manifesto_threshold: float = DEFAULT_MANIFESTO_THRESHOLD
    classifier_model: str = ""
    use_llm_classifier: bool = True
    languages: tuple[str, ...] = ("fr", "en", "de", "es", "it", "la")
    selection_seed: int = 20260731
    exploration: float = 0.05
    #: How long a licence proof stays fresh before `audit-rights`
    #: considers it stale.
    evidence_freshness_days: int = 90
    sync_interval_hours: int = 24
    export: ExportPolicy = field(default_factory=ExportPolicy)
    respect_robots: bool = True
    #: Present, and unimplemented. Documented rather than silently stubbed.
    #: The archive canary's bounds, clamped to ARCHIVE_CANARY. A config
    #: file may tighten them and may not loosen them: a "canary" that a
    #: yaml edit can turn into a full archive run is not a canary.
    canary: dict = field(default_factory=lambda: dict(ARCHIVE_CANARY))
    ocr_enabled: bool = False
    #: Per-run ceiling on OCR'd documents. Tiny by default so an
    #: accidental corpus-wide OCR run is impossible rather than merely
    #: unlikely -- OCR is the one operation here costed in CPU-hours.
    max_ocr_documents_per_run: int = 5
    user_agent_version: str = "1.0"
    raw: dict = field(default_factory=dict)

    def contact(self) -> str:
        return os.environ.get(CONTACT_ENV_VAR, "").strip()

    def user_agent(self) -> str:
        from .http import default_user_agent

        return default_user_agent(self.contact(), self.user_agent_version)


#: The archive canary's bounds. Named separately from the profile table
#: because these are the numbers an operator reads before deciding
#: whether to let the thing run at all, and burying them inside a dict
#: literal makes them easy to change by accident.
#:
#: Every one is a ceiling, not a target. `stop_on_rights_violation` is
#: the only one that is not a number, and it is the one that matters
#: most: a rights violation during a bulk run is not a statistic to
#: report at the end, it is a reason to stop immediately.
ARCHIVE_CANARY: dict[str, int] = {
    "max_items": 1000,
    "max_download_bytes": 20 * 1024**3,        # 20 GB
    "min_free_disk_bytes": 50 * 1024**3,       # 50 GB
    "max_concurrency": 1,
    "checkpoint_every_items": 25,
    "stop_on_rights_violation": True,
}


#: Acquisition profiles. `archive` is never entered implicitly: it is
#: absent unless named explicitly on the command line, and
#: `scheduler.check_budget` additionally requires a configured storage
#: budget before it will run.
ACQUISITION_PROFILES: dict[str, dict[str, Any]] = {
    "seed": {
        "description": "Small, high-confidence initial corpus. Clean formats, trusted sources only.",
        "max_items_per_run": 20,
        "min_source_trust": 0.85,
        "max_candidates_per_source": 100,
    },
    "broad": {
        "description": "Multilingual exploration with diversity quotas. The recommended daily mode.",
        "max_items_per_run": 50,
        "min_source_trust": 0.5,
        "max_candidates_per_source": 500,
    },
    "archive": {
        "description": "Bulk dumps and mirrors. Never launched implicitly; requires an explicit storage budget.",
        "max_items_per_run": 5000,
        "min_source_trust": 0.5,
        "max_candidates_per_source": 100_000,
        "requires_explicit_budget": True,
    },
    # The canary is how `archive` gets proven before anyone trusts it
    # with a real run. It is the archive profile with every dial turned
    # down far enough that a failure is cheap and visible: a thousand
    # items instead of five, one worker instead of many, and a checkpoint
    # every twenty-five items so an interruption costs minutes rather
    # than a night. Running this first is the difference between
    # discovering a resume bug at 1000 documents and discovering it at
    # 400,000.
    "archive-canary": {
        "description": (
            "A bounded rehearsal of archive mode. Run this before archive, and read "
            "its report before trusting archive with a real budget."
        ),
        "max_items_per_run": ARCHIVE_CANARY["max_items"],
        "min_source_trust": 0.5,
        "max_candidates_per_source": 5_000,
        "requires_explicit_budget": True,
        "is_canary": True,
    },
}


def _clamped_canary(raw) -> dict:
    """
    Read the canary bounds, tightening only.

    A configuration file may lower a ceiling and may not raise one, and
    may not switch off `stop_on_rights_violation`. A canary that a yaml
    edit can turn into an unbounded archive run is not a canary -- and
    the edit that does it would look entirely innocent in a diff.
    """
    bounds = dict(ARCHIVE_CANARY)
    if not isinstance(raw, dict):
        return bounds

    for key in ("max_items", "max_download_bytes", "min_free_disk_bytes",
                "max_concurrency", "checkpoint_every_items"):
        if key not in raw:
            continue
        try:
            value = int(raw[key])
        except (TypeError, ValueError):
            logger.warning("archive_canary.%s is not a number; keeping %s", key, bounds[key])
            continue
        if value <= 0:
            logger.warning("archive_canary.%s must be positive; keeping %s", key, bounds[key])
            continue
        if key == "min_free_disk_bytes":
            # A FLOOR, not a ceiling: tightening means requiring MORE
            # free disk, so the safe direction is the other way round.
            if value < bounds[key]:
                logger.warning(
                    "archive_canary.min_free_disk_bytes may only be raised; keeping %s", bounds[key]
                )
                continue
        elif value > bounds[key]:
            logger.warning(
                "archive_canary.%s may only be lowered (asked %s, ceiling %s); keeping the ceiling",
                key, value, bounds[key],
            )
            continue
        bounds[key] = value

    if raw.get("stop_on_rights_violation") is False:
        logger.warning(
            "archive_canary.stop_on_rights_violation cannot be disabled; keeping it on"
        )
    bounds["stop_on_rights_violation"] = True
    return bounds


def sources_path(cfg: AppConfig, override: str | Path | None = None) -> Path:
    return Path(override) if override else cfg.root / DEFAULT_SOURCES_PATH


def policy_path(cfg: AppConfig, override: str | Path | None = None) -> Path:
    return Path(override) if override else cfg.root / DEFAULT_POLICY_PATH


def load_policy(cfg: AppConfig, path: str | Path | None = None) -> HarvesterPolicy:
    resolved = policy_path(cfg, path)
    if not resolved.exists():
        raise PolicyError(
            f"Missing harvester policy file: {resolved}. "
            f"Run `field-horizon corpus health` for a suggested starting configuration."
        )

    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise PolicyError(f"{resolved}: expected a YAML mapping at the top level")

    quality_raw = raw.get("quality", {}) or {}
    export_raw = raw.get("export", {}) or {}
    classification_raw = raw.get("classification", {}) or {}

    return HarvesterPolicy(
        profile=str(raw.get("profile", "broad")),
        rights_profile=str(raw.get("rights_profile", "local_research_us")),
        budget=Budget.from_dict(raw.get("budget")),
        selection_weights=SelectionWeights.from_dict(raw.get("selection_weights")),
        quality=QualityThresholds(
            min_score=float(quality_raw.get("min_score", 0.45)),
            max_unreadable_ratio=float(quality_raw.get("max_unreadable_ratio", 0.01)),
            max_mojibake_ratio=float(quality_raw.get("max_mojibake_ratio", 0.002)),
            min_alpha_ratio=float(quality_raw.get("min_alpha_ratio", 0.55)),
            max_repetition_ratio=float(quality_raw.get("max_repetition_ratio", 0.35)),
            min_language_confidence=float(quality_raw.get("min_language_confidence", 0.35)),
            reject_on_language_mismatch=bool(quality_raw.get("reject_on_language_mismatch", False)),
        ),
        manifesto_threshold=float(classification_raw.get("manifesto_threshold", DEFAULT_MANIFESTO_THRESHOLD)),
        classifier_model=str(classification_raw.get("model", "") or ""),
        use_llm_classifier=bool(classification_raw.get("use_llm", True)),
        languages=tuple(raw.get("languages", ["fr", "en", "de", "es", "it", "la"])),
        selection_seed=int(raw.get("selection_seed", 20260731)),
        exploration=float(raw.get("exploration", 0.05)),
        evidence_freshness_days=int(raw.get("evidence_freshness_days", 90)),
        sync_interval_hours=int(raw.get("sync_interval_hours", 24)),
        export=ExportPolicy(
            allow_local_us_in_worldwide=bool(export_raw.get("allow_local_us_in_worldwide", False)),
            require_attribution_file=bool(export_raw.get("require_attribution_file", True)),
            include_quarantined=bool(export_raw.get("include_quarantined", False)),
        ),
        respect_robots=bool(raw.get("respect_robots", True)),
        canary=_clamped_canary(raw.get("archive_canary")),
        ocr_enabled=bool(raw.get("ocr", {}).get("enabled", False)) if isinstance(raw.get("ocr"), dict) else False,
        max_ocr_documents_per_run=int(raw.get("ocr", {}).get("max_documents_per_run", 5))
        if isinstance(raw.get("ocr"), dict) else 5,
        user_agent_version=str(raw.get("user_agent_version", "1.0")),
        raw=raw,
    )


def load_sources(cfg: AppConfig, path: str | Path | None = None) -> list[SourceConfig]:
    resolved = sources_path(cfg, path)
    if not resolved.exists():
        raise PolicyError(f"Missing harvester source configuration: {resolved}")

    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise PolicyError(f"{resolved}: expected a YAML mapping at the top level")

    entries = raw.get("sources")
    if not isinstance(entries, list):
        raise PolicyError(f"{resolved}: 'sources' must be a list")

    from .adapters.registry import available_adapters

    known_adapters = set(available_adapters())
    configs: list[SourceConfig] = []
    seen: set[str] = set()

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise PolicyError(f"{resolved}: sources[{index}] must be a mapping")

        source_id = str(entry.get("id", "")).strip()
        if not source_id:
            raise PolicyError(f"{resolved}: sources[{index}] has no id")
        if source_id in seen:
            raise PolicyError(f"{resolved}: duplicate source id {source_id!r}")
        seen.add(source_id)

        adapter = str(entry.get("adapter", "")).strip()
        enabled = bool(entry.get("enabled", False))
        if enabled and adapter not in known_adapters:
            raise PolicyError(
                f"{resolved}: source {source_id!r} is enabled but names unknown adapter {adapter!r}. "
                f"Available: {', '.join(sorted(known_adapters))}"
            )

        allowed_hosts = entry.get("allowed_hosts") or []
        if not isinstance(allowed_hosts, list) or not all(isinstance(h, str) for h in allowed_hosts):
            raise PolicyError(f"{resolved}: source {source_id!r} allowed_hosts must be a list of strings")
        if enabled and not allowed_hosts:
            # A source with no allowlist can fetch anywhere. That is never
            # an acceptable default, so it is an error rather than a
            # permissive fallback.
            raise PolicyError(
                f"{resolved}: source {source_id!r} is enabled with no allowed_hosts. "
                f"Every enabled source must declare the domains it may fetch from."
            )

        configs.append(
            SourceConfig(
                source_id=source_id,
                adapter=adapter,
                display_name=str(entry.get("name", source_id)),
                enabled=enabled,
                base_url=str(entry.get("base_url", "")),
                allowed_hosts=list(allowed_hosts),
                trust=float(entry.get("trust", 0.5)),
                trusted_for_content_rights=bool(entry.get("trusted_for_content_rights", True)),
                requests_per_minute=float(entry.get("requests_per_minute", 20.0)),
                max_download_bytes=int(entry.get("max_download_bytes", 64 * 1024 * 1024)),
                allow_http=bool(entry.get("allow_http", False)),
                languages=list(entry.get("languages", []) or []),
                options=dict(entry.get("options", {}) or {}),
                notes=str(entry.get("notes", "")),
                # Absolute, from the Field Horizon root -- never
                # cwd-relative. See SourceConfig.cache_dir.
                cache_dir=cfg.root / "data" / "corpus" / "manifests",
            )
        )

    return configs


def enabled_sources(sources: list[SourceConfig], profile: str = "broad") -> list[SourceConfig]:
    """
    Sources eligible for one acquisition profile.

    `seed` additionally filters on `min_source_trust`, which is how the
    seed profile ends up using only Standard Ebooks and Gutenberg without
    naming them: trust is a property of the source's configuration, not a
    hardcoded list here.
    """
    spec = ACQUISITION_PROFILES.get(profile, ACQUISITION_PROFILES["broad"])
    minimum = float(spec.get("min_source_trust", 0.0))
    return [s for s in sources if s.enabled and s.trust >= minimum]


def describe_profiles() -> list[dict]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "max_items_per_run": spec["max_items_per_run"],
            "min_source_trust": spec["min_source_trust"],
            "requires_explicit_budget": bool(spec.get("requires_explicit_budget")),
            "is_canary": bool(spec.get("is_canary")),
        }
        for name, spec in ACQUISITION_PROFILES.items()
    ]


__all__ = [
    "ACQUISITION_PROFILES",
    "ARCHIVE_CANARY",
    "CONTACT_ENV_VAR",
    "DEFAULT_POLICY_PATH",
    "DEFAULT_SOURCES_PATH",
    "Budget",
    "ExportPolicy",
    "HarvesterPolicy",
    "PolicyError",
    "describe_profiles",
    "enabled_sources",
    "load_policy",
    "load_sources",
    "policy_path",
    "sources_path",
]
