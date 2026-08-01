"""Warehouse Hook handler."""

from contracts import Result


def handle(records):
    """Summarise warehouse_hook records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "warehouse_hook":
            continue
        total += 1
    return Result(name="warehouse_hook", total=total)
