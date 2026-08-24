"""Warehouse Queue handler."""

from contracts import Result


def handle(records):
    """Summarise warehouse_queue records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "warehouse_queue":
            continue
        total += 1
    return Result(name="warehouse_queue", total=total)
