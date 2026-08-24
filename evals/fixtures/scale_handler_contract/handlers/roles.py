"""Roles domain handler."""

from contracts import Result


def handle(records):
    """Summarise roles records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "roles":
            continue
        total += 1
    return Result(name="roles", total=total)
