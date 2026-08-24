"""Customer Hook handler."""

from contracts import Result


def handle(records):
    """Summarise customer_hook records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "customer_hook":
            continue
        total += 1
    return Result(name="customer_hook", total=total)
