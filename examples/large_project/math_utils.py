"""Mathematical and statistical utility functions."""

from __future__ import annotations

import math


def safe_divide(a: float, b: float) -> float:
    """Division that returns 0.0 when the divisor is zero."""
    if b == 0:
        return 0.0
    return a / b


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp a value to the [lo, hi] range."""
    return max(lo, min(hi, value))


def weighted_average(values: list[float], weights: list[float]) -> float:
    """Compute the weighted average of a list of values."""
    total_weight = sum(weights)
    if total_weight == 0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / total_weight


def standard_deviation(values: list[float]) -> float:
    """Compute the sample standard deviation."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance)


def normalize(values: list[float]) -> list[float]:
    """Min-max normalize values to [0, 1]."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    spread = hi - lo
    if spread == 0:
        return [0.5] * len(values)
    return [(v - lo) / spread for v in values]


def percentile(values: list[float], p: float) -> float:
    """Compute the p-th percentile (0-100) using linear interpolation."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * clamp(p, 0, 100) / 100
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def z_scores(values: list[float]) -> list[float]:
    """Compute z-scores for each value."""
    if len(values) < 2:
        return [0.0] * len(values)
    mean = sum(values) / len(values)
    sd = standard_deviation(values)
    return [safe_divide(v - mean, sd) for v in values]
