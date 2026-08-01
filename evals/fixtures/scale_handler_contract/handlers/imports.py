"""Imports domain handler."""

from contracts import Result


def handle(records):
    """Summarise imports records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "imports":
            continue
        total += 1
    return Result(name="imports", total=total)
