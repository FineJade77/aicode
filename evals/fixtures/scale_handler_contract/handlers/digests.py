"""Digests domain handler."""

from contracts import Result


def handle(records):
    """Summarise digests records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "digests":
            continue
        total += 1
    return Result(name="digests", total=total)
