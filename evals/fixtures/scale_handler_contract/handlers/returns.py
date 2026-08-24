"""Returns domain handler."""

from contracts import Result


def handle(records):
    """Summarise returns records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "returns":
            continue
        total += 1
    return Result(name="returns", total=total)
