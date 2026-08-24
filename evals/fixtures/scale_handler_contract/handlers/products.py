"""Products domain handler."""

from contracts import Result


def handle(records):
    """Summarise products records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "products":
            continue
        total += 1
    return Result(name="products", total=total)
