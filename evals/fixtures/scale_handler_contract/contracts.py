"""Shared handler contract.

Every domain handler returns a `Result`. `total` is a **count of records**, so
the aggregator can sum totals across handlers and compare against the number of
records it dispatched.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Result:
    name: str
    total: int
