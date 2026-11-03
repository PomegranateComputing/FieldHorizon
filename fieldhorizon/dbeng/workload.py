"""
Workloads: what is measured, and the runner that measures it.

A workload is a versioned YAML file of parameterised queries with weights
(relative execution frequency) and a kind (read, or write -- a write is
measured inside a transaction that is always rolled back). Parameters are
generated from a seed, so the before and after measurements run exactly
the same statements.

Measurement discipline (anti-"lucky run"):
* warm-up executions are discarded;
* every query runs once per parameter set per round, rounds rotate the
  query order, and all samples are kept in round order;
* the server's own "Execution Time" from EXPLAIN ANALYZE is used, free of
  client and network jitter, together with the plan that produced it;
* optionally (policy measurement.client_timing) the plain statement is also
  executed and timed client-side, reported beside -- never instead of -- the
  server time;
* control queries (`control: true`) are ones the candidate index cannot
  serve: if they move, the environment drifted (policy max_control_drift).

Side effects, stated rather than hidden: write queries are rolled back, yet
each execution still leaves dead tuples and consumes identity values; the
operation records state snapshots around every measurement to show them.
"""
from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .model import ExplainResult

KIND_READ = "read"
KIND_WRITE = "write"


@dataclass(frozen=True)
class WorkloadQuery:
    query_id: str
    kind: str
    weight: float
    sql: str
    params: tuple[dict, ...]
    repeat: int = 1
    control: bool = False


@dataclass(frozen=True)
class Workload:
    name: str
    version: int
    sha256: str
    source: str
    queries: tuple[WorkloadQuery, ...]

    def roles(self) -> dict[str, str]:
        return {q.query_id: q.kind for q in self.queries}

    def weights(self) -> dict[str, float]:
        return {q.query_id: q.weight for q in self.queries}

    def controls(self) -> list[str]:
        return [q.query_id for q in self.queries if q.control]


def _expand_params(spec: object, query_id: str) -> tuple[dict, ...]:
    if spec is None:
        return ({},)
    if isinstance(spec, list):
        return tuple(dict(p) for p in spec)
    if isinstance(spec, dict):
        generated: list[dict] = []
        count = None
        columns: dict[str, list] = {}
        for name, gen in spec.items():
            if not isinstance(gen, dict) or "int_range" not in gen:
                raise ValueError(f"{query_id}: parameter {name!r} needs int_range/count/seed")
            lo, hi = gen["int_range"]
            n = int(gen["count"])
            rng = random.Random(int(gen.get("seed", 0)))
            columns[name] = [rng.randint(int(lo), int(hi)) for _ in range(n)]
            count = n if count is None else min(count, n)
        for i in range(count or 0):
            generated.append({name: values[i] for name, values in columns.items()})
        return tuple(generated)
    raise ValueError(f"{query_id}: params must be a list of mappings or a generator mapping")


def load_workload(path: Path) -> Workload:
    data = path.read_bytes()
    raw = yaml.safe_load(data) or {}
    queries = []
    for q in raw.get("queries", []):
        kind = q.get("kind", KIND_READ)
        if kind not in (KIND_READ, KIND_WRITE):
            raise ValueError(f"{q.get('id')}: kind must be read or write")
        queries.append(WorkloadQuery(
            query_id=str(q["id"]), kind=kind, weight=float(q.get("weight", 1.0)),
            sql=str(q["sql"]).strip(), params=_expand_params(q.get("params"), str(q["id"])),
            repeat=max(1, int(q.get("repeat", 1))), control=bool(q.get("control", False)),
        ))
    if not queries:
        raise ValueError(f"{path}: no queries")
    return Workload(
        name=str(raw.get("name", path.stem)), version=int(raw.get("workload_version", 1)),
        sha256=hashlib.sha256(data).hexdigest(), source=str(path), queries=tuple(queries),
    )


@dataclass
class Sample:
    query_id: str
    round: int
    param_index: int
    execution_ms: float
    planning_ms: float
    indexes_used: list[str]
    seq_scan_relations: list[str]
    client_ms: float | None = None


@dataclass
class Measurement:
    label: str
    rounds: int
    samples: list[Sample] = field(default_factory=list)
    first_plans: dict[str, ExplainResult] = field(default_factory=dict)

    def series(self) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for s in self.samples:
            out.setdefault(s.query_id, []).append(s.execution_ms)
        return out

    def client_series(self) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for s in self.samples:
            if s.client_ms is not None:
                out.setdefault(s.query_id, []).append(s.client_ms)
        return out

    def index_usage(self, index_name: str) -> dict[str, float]:
        usage: dict[str, list[bool]] = {}
        for s in self.samples:
            usage.setdefault(s.query_id, []).append(index_name in s.indexes_used)
        return {q: sum(v) / len(v) for q, v in usage.items() if v}

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "rounds": self.rounds,
            "samples": [s.__dict__ for s in self.samples],
            "first_plans": {q: p.plan for q, p in self.first_plans.items()},
        }


def run_workload(
    adapter,
    workload: Workload,
    *,
    rounds: int,
    warmup_runs: int,
    label: str,
    on_round: Callable[[int], None] | None = None,
    client_timing: bool = False,
) -> Measurement:
    """``on_round``, called before every round, is the operation's safe point: it may pause, or raise to stop."""
    measurement = Measurement(label=label, rounds=rounds)
    for query in workload.queries:
        for _ in range(warmup_runs):
            for params in query.params:
                adapter.explain(query.sql, params, write=query.kind == KIND_WRITE)
    queries = list(workload.queries)
    for r in range(rounds):
        if on_round:
            on_round(r)
        order = queries[r % len(queries):] + queries[: r % len(queries)]
        for query in order:
            for i, params in [(i, p) for i, p in enumerate(query.params) for _ in range(query.repeat)]:
                result = adapter.explain(query.sql, params, write=query.kind == KIND_WRITE)
                client_ms = adapter.execute_timed(query.sql, params, write=query.kind == KIND_WRITE) if client_timing else None
                if query.query_id not in measurement.first_plans:
                    measurement.first_plans[query.query_id] = result
                measurement.samples.append(Sample(
                    query_id=query.query_id, round=r, param_index=i,
                    execution_ms=result.execution_ms, planning_ms=result.planning_ms,
                    indexes_used=list(result.indexes_used),
                    seq_scan_relations=[s["relation"] for s in result.seq_scans],
                    client_ms=round(client_ms, 4) if client_ms is not None else None,
                ))
    return measurement
