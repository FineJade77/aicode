"""Discount Store handler."""

from contracts import Result


def handle(records):
    """Summarise discount_store records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "discount_store":
            continue
        total += 1
    return Result(name="discount_store", total=total)
