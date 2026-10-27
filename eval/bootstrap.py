"""Paired bootstrap confidence intervals.

n=30..100 is small. A two-point difference is usually noise; reporting a point
estimate without an interval is how evaluation reports lie by accident.
"""

from __future__ import annotations

import random
from collections.abc import Sequence


def percentile_ci(
    values: Sequence[float], *, n_boot: int = 1000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    if len(values) == 1:
        return (float(values[0]), float(values[0]))
    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(n_boot):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lower = means[int((alpha / 2) * n_boot)]
    upper = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (lower, upper)


def paired_delta_ci(
    baseline: Sequence[float],
    current: Sequence[float],
    *,
    n_boot: int = 1000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """CI for ``mean(current) - mean(baseline)`` using the same items."""
    if len(baseline) != len(current):
        raise ValueError("paired bootstrap needs equal-length, same-order samples")
    if not baseline:
        return (0.0, 0.0)
    deltas = [c - b for b, c in zip(baseline, current, strict=True)]
    return percentile_ci(deltas, n_boot=n_boot, alpha=alpha, seed=seed)
