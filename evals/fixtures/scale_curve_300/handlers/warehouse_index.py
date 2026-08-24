"""Warehouse Index handler."""

from contracts import Result


def handle(records):
    """Summarise warehouse_index records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "warehouse_index":
            continue
        total += 1
    return Result(name="warehouse_index", total=total)
