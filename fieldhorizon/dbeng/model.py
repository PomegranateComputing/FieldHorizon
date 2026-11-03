"""Engine-neutral value types shared by adapters, advisor, policy and operations."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# Feature availability vocabulary used throughout snapshots and evidence: a
# missing statistic is never silently treated as zero or as "fine".
AVAILABLE = "available"
UNAVAILABLE = "unavailable"      # exists in principle, could not be read (not installed, no privilege)
UNSUPPORTED = "unsupported"      # this engine/version/object kind does not offer it
UNVERIFIED = "unverified"        # read, but a relationship could not be classified or confirmed

MAX_IDENTIFIER = 63  # PostgreSQL NAMEDATALEN - 1


@dataclass(frozen=True)
class IndexSpec:
    schema: str
    table: str
    columns: tuple[str, ...]
    method: str = "btree"

    @property
    def key(self) -> str:
        return f"{self.schema}.{self.table}({', '.join(self.columns)})"

    @property
    def name(self) -> str:
        """Deterministic, so re-running an operation finds its own index instead of creating a twin."""
        digest = hashlib.sha256(f"{self.key}|{self.method}".encode()).hexdigest()[:8]
        stem = f"fhx_{self.table}_{'_'.join(self.columns)}"
        return f"{stem[: MAX_IDENTIFIER - 9]}_{digest}"


@dataclass
class ExplainResult:
    execution_ms: float
    planning_ms: float
    indexes_used: list[str]
    node_types: list[str]
    seq_scans: list[dict]
    plan: dict


@dataclass
class IndexState:
    schema: str
    name: str
    table: str
    valid: bool
    ready: bool
    definition: str
    marker: dict | None
    size_bytes: int


@dataclass
class Candidate:
    candidate_id: str
    spec: IndexSpec
    source: str                      # "advisor" | "agent:<role>" | "operator"
    rationale: str
    evidence_refs: list[str] = field(default_factory=list)
    target_queries: list[str] = field(default_factory=list)
    equality_columns: list[str] = field(default_factory=list)
    range_columns: list[str] = field(default_factory=list)

    def describe(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "key": self.spec.key,
            "index_name": self.spec.name,
            "schema": self.spec.schema,
            "table": self.spec.table,
            "columns": list(self.spec.columns),
            "method": self.spec.method,
            "source": self.source,
            "rationale": self.rationale,
            "evidence_refs": self.evidence_refs,
            "target_queries": self.target_queries,
            "equality_columns": self.equality_columns,
            "range_columns": self.range_columns,
        }
