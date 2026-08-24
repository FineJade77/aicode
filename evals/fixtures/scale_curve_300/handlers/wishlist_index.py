"""Wishlist Index handler."""

from contracts import Result


def handle(records):
    """Summarise wishlist_index records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "wishlist_index":
            continue
        total += 1
    return Result(name="wishlist_index", total=total)
