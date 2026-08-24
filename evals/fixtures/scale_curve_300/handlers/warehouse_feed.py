"""Warehouse Feed handler."""

from contracts import Result


def handle(records):
    """Summarise warehouse_feed records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "warehouse_feed":
            continue
        total += 1
    return Result(name="warehouse_feed", total=total)
