from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import AppConfig


class ManifestError(ValueError):
    """data/sources.yaml is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class SourceManifestEntry:
    id: str
    title: str
    path: str
    author: str | None = None
    year: int | None = None
    language: str = "unknown"
    license: str = "unknown"
    domain_hints: tuple[str, ...] = ()
    weight: float = 1.0
    notes: str = ""

    def resolve_path(self, cfg: AppConfig) -> Path:
        return cfg.root / self.path


def _require_str(entry: dict, key: str, index: int) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"sources[{index}].{key} must be a non-empty string")
    return value


def _optional_str(entry: dict, key: str, index: int, default: str) -> str:
    value = entry.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ManifestError(f"sources[{index}].{key} must be a string or null")
    return value


def _optional_int(entry: dict, key: str, index: int) -> int | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ManifestError(f"sources[{index}].{key} must be an integer or null")
    return value


def _optional_domain_hints(entry: dict, index: int) -> tuple[str, ...]:
    value = entry.get("domain_hints") or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"sources[{index}].domain_hints must be a list of strings")
    return tuple(value)


def _weight(entry: dict, index: int) -> float:
    value = entry.get("weight", 1.0)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ManifestError(f"sources[{index}].weight must be a number")
    weight = float(value)
    if weight <= 0:
        raise ManifestError(f"sources[{index}].weight must be positive, got {weight}")
    return weight


def _parse_entry(raw: object, index: int) -> SourceManifestEntry:
    if not isinstance(raw, dict):
        raise ManifestError(f"sources[{index}] must be a mapping, got {type(raw).__name__}")

    return SourceManifestEntry(
        id=_require_str(raw, "id", index),
        title=_require_str(raw, "title", index),
        path=_require_str(raw, "path", index),
        author=_optional_str(raw, "author", index, "") or None,
        year=_optional_int(raw, "year", index),
        language=_optional_str(raw, "language", index, "unknown"),
        license=_optional_str(raw, "license", index, "unknown"),
        domain_hints=_optional_domain_hints(raw, index),
        weight=_weight(raw, index),
        notes=_optional_str(raw, "notes", index, ""),
    )


def load_manifest(path: Path) -> list[SourceManifestEntry]:
    """
    Loads and validates data/sources.yaml (Civilization Engine corpus-scale
    phase): curation as code. Every entry is checked structurally here
    (required fields, types, positive weight, unique id) so a malformed
    manifest fails loudly and precisely, before `ingest-manifest` ever
    touches the database. File *existence* at `path` is deliberately not
    checked here -- that's `ingest_from_manifest`'s job, reported per
    source rather than failing the whole load.
    """
    if not path.exists():
        raise ManifestError(f"Missing manifest file: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: expected a YAML mapping at the top level")

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list):
        raise ManifestError(f"{path}: 'sources' must be a list")

    entries = [_parse_entry(item, i) for i, item in enumerate(raw_sources)]

    seen_ids: set[str] = set()
    for entry in entries:
        if entry.id in seen_ids:
            raise ManifestError(f"{path}: duplicate source id {entry.id!r}")
        seen_ids.add(entry.id)

    return entries
