"""Order Cache handler."""

from contracts import Result


def handle(records):
    """Summarise order_cache records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "order_cache":
            continue
        total += 1
    return Result(name="order_cache", total=total)
