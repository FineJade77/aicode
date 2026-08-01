"""Product Stream handler."""

from contracts import Result


def handle(records):
    """Summarise product_stream records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "product_stream":
            continue
        total += 1
    return Result(name="product_stream", total=total)
