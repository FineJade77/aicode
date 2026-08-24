"""Wishlist Log handler."""

from contracts import Result


def handle(records):
    """Summarise wishlist_log records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "wishlist_log":
            continue
        total += 1
    return Result(name="wishlist_log", total=total)
