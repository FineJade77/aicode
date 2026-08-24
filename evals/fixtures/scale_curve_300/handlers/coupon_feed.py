"""Coupon Feed handler."""

from contracts import Result


def handle(records):
    """Summarise coupon_feed records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "coupon_feed":
            continue
        total += 1
    return Result(name="coupon_feed", total=total)
