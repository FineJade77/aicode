"""Order Stream handler."""

from contracts import Result


def handle(records):
    """Summarise order_stream records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "order_stream":
            continue
        total += 1
    return Result(name="order_stream", total=total)
