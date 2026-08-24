"""Receipt Cache handler."""

from contracts import Result


def handle(records):
    """Summarise receipt_cache records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "receipt_cache":
            continue
        total += 1
    return Result(name="receipt_cache", total=total)
