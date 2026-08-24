"""Receipts domain handler."""

from contracts import Result


def handle(records):
    """Summarise receipts records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "receipts":
            continue
        total += 1
    return Result(name="receipts", total=total)
