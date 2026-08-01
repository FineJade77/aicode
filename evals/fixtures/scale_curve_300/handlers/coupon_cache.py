"""Coupon Cache handler."""

from contracts import Result


def handle(records):
    """Summarise coupon_cache records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "coupon_cache":
            continue
        total += 1
    return Result(name="coupon_cache", total=total)
