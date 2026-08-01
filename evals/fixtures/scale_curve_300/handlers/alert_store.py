"""Alert Store handler."""

from contracts import Result


def handle(records):
    """Summarise alert_store records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "alert_store":
            continue
        total += 1
    return Result(name="alert_store", total=total)
