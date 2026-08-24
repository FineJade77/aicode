"""Discounts domain handler."""

from contracts import Result


def handle(records):
    """Summarise discounts records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "discounts":
            continue
        total += 1
    return Result(name="discounts", total=total)
