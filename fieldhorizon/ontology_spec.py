from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

# ontology.yaml lives at the repo root, alongside config.yaml -- it's part
# of the shipped code (the declarative ontology), not user data, so it is
# resolved relative to this file's own location rather than AppConfig.root.
DEFAULT_ONTOLOGY_PATH = Path(__file__).resolve().parent.parent / "ontology.yaml"


class OntologySpecError(ValueError):
    """ontology.yaml is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class DomainSpec:
    name: str
    hints: frozenset[str] = field(default_factory=frozenset)
    expansions: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    rewrite_directive: str | None = None


@dataclass(frozen=True)
class OppositionSpec:
    a: str
    b: str
    reason: str


@dataclass(frozen=True)
class WeatherAxisSpec:
    """
    One doctrinal-weather axis (Civilization Engine observability phase):
    positive/negative anchor statements, written in the corpus's own
    register, that fieldhorizon.weather embeds and differences into an
    axis vector. Read-only analytics -- this spec is never consulted by
    retrieval, evaluation, or generation.
    """
    name: str
    positive_anchors: tuple[str, ...]
    negative_anchors: tuple[str, ...]


@dataclass(frozen=True)
class OntologySpec:
    domains: dict[str, DomainSpec]
    oppositions: tuple[OppositionSpec, ...]
    source_expansions: dict[str, tuple[str, ...]]
    weather_axes: dict[str, WeatherAxisSpec]

    def weather_axis(self, name: str) -> WeatherAxisSpec | None:
        return self.weather_axes.get(name)

    def all_weather_axis_names(self) -> tuple[str, ...]:
        return tuple(self.weather_axes.keys())

    def domain(self, name: str) -> DomainSpec | None:
        return self.domains.get(name)

    def all_domain_names(self) -> tuple[str, ...]:
        return tuple(self.domains.keys())

    def hints_for(self, name: str) -> frozenset[str]:
        spec = self.domains.get(name)
        return spec.hints if spec else frozenset()

    def keywords_for(self, category: str) -> tuple[str, ...]:
        spec = self.domains.get(category)
        return spec.keywords if spec else ()

    def rewrite_directive_for(self, category: str) -> str | None:
        """
        A category triggers its own domain's directive, or -- mirroring the
        pre-unification "if categories include X" grouping in rewrite.py,
        e.g. elite_capture triggering the culture_war directive -- any
        domain whose hints list includes it.
        """
        direct = self.domains.get(category)
        if direct and direct.rewrite_directive:
            return direct.rewrite_directive

        for spec in self.domains.values():
            if category in spec.hints and spec.rewrite_directive:
                return spec.rewrite_directive

        return None

    def source_expansions_for(self, source_type: str) -> tuple[str, ...]:
        return self.source_expansions.get(source_type, ())

    def opposition_reason(self, category_a: str, category_b: str) -> str | None:
        for opp in self.oppositions:
            if (opp.a, opp.b) in ((category_a, category_b), (category_b, category_a)):
                return opp.reason
        return None


def _require_str_list(container: dict, key: str, context: str) -> tuple[str, ...]:
    value = container.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise OntologySpecError(f"{context}.{key} must be a list of strings")
    return tuple(value)


def _parse_domain(name: str, raw: object) -> DomainSpec:
    if not isinstance(raw, dict):
        raise OntologySpecError(f"domains.{name} must be a mapping, got {type(raw).__name__}")

    rewrite_directive = raw.get("rewrite_directive")
    if rewrite_directive is not None and not isinstance(rewrite_directive, str):
        raise OntologySpecError(f"domains.{name}.rewrite_directive must be a string or null")

    return DomainSpec(
        name=name,
        hints=frozenset(_require_str_list(raw, "hints", f"domains.{name}")),
        expansions=_require_str_list(raw, "expansions", f"domains.{name}"),
        sources=_require_str_list(raw, "sources", f"domains.{name}"),
        keywords=_require_str_list(raw, "keywords", f"domains.{name}"),
        rewrite_directive=rewrite_directive,
    )


def _parse_weather_axis(name: str, raw: object) -> WeatherAxisSpec:
    if not isinstance(raw, dict):
        raise OntologySpecError(f"weather.{name} must be a mapping, got {type(raw).__name__}")

    positive = _require_str_list(raw, "positive", f"weather.{name}")
    negative = _require_str_list(raw, "negative", f"weather.{name}")

    if not (3 <= len(positive) <= 5):
        raise OntologySpecError(f"weather.{name}.positive must have 3-5 anchor statements, got {len(positive)}")
    if not (3 <= len(negative) <= 5):
        raise OntologySpecError(f"weather.{name}.negative must have 3-5 anchor statements, got {len(negative)}")

    return WeatherAxisSpec(name=name, positive_anchors=positive, negative_anchors=negative)


def _parse_opposition(index: int, raw: object) -> OppositionSpec:
    if not isinstance(raw, list) or len(raw) != 3:
        raise OntologySpecError(f"oppositions[{index}] must be a 3-item list [category_a, category_b, reason]")

    category_a, category_b, reason = raw
    if not all(isinstance(x, str) for x in (category_a, category_b, reason)):
        raise OntologySpecError(f"oppositions[{index}] entries must all be strings")

    return OppositionSpec(a=category_a, b=category_b, reason=reason)


def load_ontology(path: Path | str = DEFAULT_ONTOLOGY_PATH) -> OntologySpec:
    path = Path(path)
    if not path.exists():
        raise OntologySpecError(f"Missing ontology file: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise OntologySpecError(f"{path}: expected a YAML mapping at the top level")

    raw_domains = raw.get("domains") or {}
    if not isinstance(raw_domains, dict):
        raise OntologySpecError(f"{path}: 'domains' must be a mapping")
    domains = {name: _parse_domain(name, entry) for name, entry in raw_domains.items()}

    raw_oppositions = raw.get("oppositions") or []
    if not isinstance(raw_oppositions, list):
        raise OntologySpecError(f"{path}: 'oppositions' must be a list")
    oppositions = tuple(_parse_opposition(i, entry) for i, entry in enumerate(raw_oppositions))

    raw_source_expansions = raw.get("source_expansions") or {}
    if not isinstance(raw_source_expansions, dict):
        raise OntologySpecError(f"{path}: 'source_expansions' must be a mapping")
    source_expansions = {
        source_type: _require_str_list(raw_source_expansions, source_type, "source_expansions")
        for source_type in raw_source_expansions
    }

    raw_weather = raw.get("weather") or {}
    if not isinstance(raw_weather, dict):
        raise OntologySpecError(f"{path}: 'weather' must be a mapping")
    weather_axes = {name: _parse_weather_axis(name, entry) for name, entry in raw_weather.items()}

    return OntologySpec(
        domains=domains,
        oppositions=oppositions,
        source_expansions=source_expansions,
        weather_axes=weather_axes,
    )


@lru_cache(maxsize=8)
def _cached_load(path_str: str) -> OntologySpec:
    return load_ontology(Path(path_str))


def clear_ontology_cache() -> None:
    """
    For the one caller that legitimately changes ontology.yaml on disk after
    process start: proposals.apply_proposal (Civilization Engine dream
    phase), so a freshly merged domain is visible without a restart. No
    other code path modifies ontology.yaml, so no other caller needs this.
    """
    _cached_load.cache_clear()


def get_ontology(path: Path | str | None = None) -> OntologySpec:
    """
    Cached accessor -- ontology.yaml is read from disk once per distinct
    path per process. Pass `path` explicitly (e.g. in tests) to load a
    different ontology file; omit it to use the repo's own ontology.yaml.
    """
    resolved = Path(path) if path is not None else DEFAULT_ONTOLOGY_PATH
    return _cached_load(str(resolved))
