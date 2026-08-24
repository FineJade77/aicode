"""Warehouse Cache handler."""

from contracts import Result


def handle(records):
    """Summarise warehouse_cache records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "warehouse_cache":
            continue
        total += 1
    return Result(name="warehouse_cache", total=total)
