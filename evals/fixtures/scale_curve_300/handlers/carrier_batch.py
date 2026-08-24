"""Carrier Batch handler."""

from contracts import Result


def handle(records):
    """Summarise carrier_batch records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "carrier_batch":
            continue
        total += 1
    return Result(name="carrier_batch", total=total)
