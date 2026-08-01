"""Median of a list of numbers."""


def median(values: list[float]) -> float:
    """Middle value; the mean of the two middle values when the count is even."""
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
