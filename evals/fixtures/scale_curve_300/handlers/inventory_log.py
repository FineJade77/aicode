"""Inventory Log handler."""

from contracts import Result


def handle(records):
    """Summarise inventory_log records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "inventory_log":
            continue
        total += 1
    return Result(name="inventory_log", total=total)
