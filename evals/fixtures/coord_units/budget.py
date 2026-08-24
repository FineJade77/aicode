"""Aggregate time spent."""

from timing import elapsed


def total_minutes(spans):
    return sum(elapsed(start, end) for start, end in spans) / 60
