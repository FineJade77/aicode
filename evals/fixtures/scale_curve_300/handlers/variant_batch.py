"""Variant Batch handler."""

from contracts import Result


def handle(records):
    """Summarise variant_batch records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "variant_batch":
            continue
        total += 1
    return Result(name="variant_batch", total=total)
