"""Slow-run alerting."""

from timing import elapsed

SLOW_THRESHOLD = 2.5


def is_slow(start, end):
    return elapsed(start, end) > SLOW_THRESHOLD
