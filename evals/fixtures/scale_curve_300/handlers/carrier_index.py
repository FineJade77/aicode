"""Carrier Index handler."""

from contracts import Result


def handle(records):
    """Summarise carrier_index records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "carrier_index":
            continue
        total += 1
    return Result(name="carrier_index", total=total)
