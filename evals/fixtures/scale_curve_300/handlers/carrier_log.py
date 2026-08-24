"""Carrier Log handler."""

from contracts import Result


def handle(records):
    """Summarise carrier_log records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "carrier_log":
            continue
        total += 1
    return Result(name="carrier_log", total=total)
