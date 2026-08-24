"""Wishlist Stream handler."""

from contracts import Result


def handle(records):
    """Summarise wishlist_stream records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "wishlist_stream":
            continue
        total += 1
    return Result(name="wishlist_stream", total=total)
