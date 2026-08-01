"""Alert Feed handler."""

from contracts import Result


def handle(records):
    """Summarise alert_feed records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "alert_feed":
            continue
        total += 1
    return Result(name="alert_feed", total=total)
