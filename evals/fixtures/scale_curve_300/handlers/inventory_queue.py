"""Inventory Queue handler."""

from contracts import Result


def handle(records):
    """Summarise inventory_queue records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "inventory_queue":
            continue
        total += 1
    return Result(name="inventory_queue", total=total)
