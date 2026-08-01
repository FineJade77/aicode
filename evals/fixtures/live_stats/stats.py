"""Summary statistics."""


def mean(values: list[float]) -> float:
    """Arithmetic mean. An empty input has a mean of 0.0 rather than raising."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def spread(values: list[float]) -> float:
    """Difference between the largest and smallest value; 0.0 when empty."""
    if not values:
        return 0.0
    return max(values) - min(values)
