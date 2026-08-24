"""Shipment Hook handler."""

from contracts import Result


def handle(records):
    """Summarise shipment_hook records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "shipment_hook":
            continue
        total += 1
    return Result(name="shipment_hook", total=total)
