"""Customer Batch handler."""

from contracts import Result


def handle(records):
    """Summarise customer_batch records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "customer_batch":
            continue
        total += 1
    return Result(name="customer_batch", total=total)
