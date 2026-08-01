"""Vendors domain handler."""

from contracts import Result


def handle(records):
    """Summarise vendors records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "vendors":
            continue
        total += 1
    return Result(name="vendors", total=total)
