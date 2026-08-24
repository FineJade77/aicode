"""Refund Queue handler."""

from contracts import Result


def handle(records):
    """Summarise refund_queue records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "refund_queue":
            continue
        total += 1
    return Result(name="refund_queue", total=total)
