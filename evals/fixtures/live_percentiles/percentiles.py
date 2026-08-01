"""Nearest-rank percentiles."""

import math


def percentile(values: list[float], percent: float) -> float:
    """Nearest-rank percentile of a non-empty list of values."""
    ordered = sorted(values)
    index = math.ceil(percent / 100 * len(ordered))
    return ordered[index]
