"""Human-readable run reports."""

from timing import elapsed


def format_duration(start, end):
    return f"{elapsed(start, end):.1f}s"
