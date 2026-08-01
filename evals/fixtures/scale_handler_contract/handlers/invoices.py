"""Invoices domain handler."""

from contracts import Result


def handle(records):
    """Summarise invoices records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "invoices":
            continue
        total += 1
    return Result(name="invoices", total=total)
