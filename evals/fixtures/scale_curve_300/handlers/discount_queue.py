"""Discount Queue handler."""

from contracts import Result


def handle(records):
    """Summarise discount_queue records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "discount_queue":
            continue
        total += 1
    return Result(name="discount_queue", total=total)
