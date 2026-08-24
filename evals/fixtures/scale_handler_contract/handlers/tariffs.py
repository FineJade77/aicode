"""Tariffs domain handler."""

from contracts import Result


def handle(records):
    """Summarise tariffs records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "tariffs":
            continue
        total += record.get('amount', 1)
    return Result(name="tariffs", total=total)
