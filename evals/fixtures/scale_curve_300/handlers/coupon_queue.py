"""Coupon Queue handler."""

from contracts import Result


def handle(records):
    """Summarise coupon_queue records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "coupon_queue":
            continue
        total += 1
    return Result(name="coupon_queue", total=total)
