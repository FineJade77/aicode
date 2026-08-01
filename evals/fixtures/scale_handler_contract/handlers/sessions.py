"""Sessions domain handler."""

from contracts import Result


def handle(records):
    """Summarise sessions records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "sessions":
            continue
        total += 1
    return Result(name="sessions", total=total)
