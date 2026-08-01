"""Carriers domain handler."""

from contracts import Result


def handle(records):
    """Summarise carriers records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "carriers":
            continue
        total += 1
    return Result(name="carriers", total=total)
