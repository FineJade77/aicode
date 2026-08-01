"""Aggregate handler results."""

from registry import dispatch


def summarise(records):
    results = dispatch(records)
    return {
        "handlers": len(results),
        "processed": sum(result.total for result in results),
    }
