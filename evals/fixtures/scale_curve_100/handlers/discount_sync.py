"""Discount Sync handler."""

from contracts import Result


def handle(records):
    """Summarise discount_sync records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "discount_sync":
            continue
        total += 1
    return Result(name="discount_sync", total=total)
