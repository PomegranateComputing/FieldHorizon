"""
Deterministic statistics for before/after timing samples.

No randomness that is not seeded, no dependency beyond numpy: the same
samples always produce the same verdict, so a decision recorded in the
journal can be re-derived from its evidence by anyone.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

BOOTSTRAP_SEED = 20261103
BOOTSTRAP_RESAMPLES = 2000


@dataclass(frozen=True)
class SampleSummary:
    n: int
    median: float
    p25: float
    p75: float
    minimum: float
    maximum: float
    mean: float

    @property
    def iqr(self) -> float:
        return self.p75 - self.p25


def summarize(samples: list[float]) -> SampleSummary:
    if not samples:
        raise ValueError("no samples")
    arr = np.asarray(samples, dtype=float)
    return SampleSummary(
        n=int(arr.size),
        median=float(np.median(arr)),
        p25=float(np.percentile(arr, 25)),
        p75=float(np.percentile(arr, 75)),
        minimum=float(arr.min()),
        maximum=float(arr.max()),
        mean=float(arr.mean()),
    )


def _normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def mann_whitney_less(x: list[float], y: list[float]) -> float:
    """
    One-sided p-value for H1: values in ``x`` tend to be smaller than in
    ``y`` (normal approximation with tie and continuity corrections).
    Rank-based, so one extreme outlier cannot carry the result the way it
    could carry a mean.
    """
    n1, n2 = len(x), len(y)
    if n1 == 0 or n2 == 0:
        return 1.0
    combined = np.concatenate([np.asarray(x, float), np.asarray(y, float)])
    order = combined.argsort(kind="mergesort")
    ranks = np.empty(len(combined), dtype=float)
    sorted_vals = combined[order]
    i = 0
    tie_term = 0.0
    while i < len(sorted_vals):
        j = i
        while j + 1 < len(sorted_vals) and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        ranks[order[i : j + 1]] = avg_rank
        t = j - i + 1
        tie_term += t**3 - t
        i = j + 1
    r1 = float(ranks[:n1].sum())
    u1 = r1 - n1 * (n1 + 1) / 2.0
    n = n1 + n2
    mean_u = n1 * n2 / 2.0
    var_u = n1 * n2 / 12.0 * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0.0
    if var_u <= 0:
        return 1.0
    # Small U means x ranks low, i.e. x is smaller. Continuity-corrected.
    z = (mean_u - u1 - 0.5) / math.sqrt(var_u)
    return _normal_sf(z)


def bootstrap_median_ratio_ci(before: list[float], after: list[float], level: float = 0.95) -> tuple[float, float]:
    """Seeded percentile-bootstrap interval for median(after) / median(before)."""
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    b = np.asarray(before, float)
    a = np.asarray(after, float)
    rb = np.median(rng.choice(b, size=(BOOTSTRAP_RESAMPLES, b.size), replace=True), axis=1)
    ra = np.median(rng.choice(a, size=(BOOTSTRAP_RESAMPLES, a.size), replace=True), axis=1)
    ratios = ra / np.where(rb == 0, np.finfo(float).tiny, rb)
    tail = (1.0 - level) / 2.0 * 100
    return float(np.percentile(ratios, tail)), float(np.percentile(ratios, 100 - tail))
