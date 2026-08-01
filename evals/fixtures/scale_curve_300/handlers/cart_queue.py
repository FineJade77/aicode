"""Cart Queue handler."""

from contracts import Result


def handle(records):
    """Summarise cart_queue records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "cart_queue":
            continue
        total += 1
    return Result(name="cart_queue", total=total)
