"""Webhooks domain handler."""

from contracts import Result


def handle(records):
    """Summarise webhooks records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "webhooks":
            continue
        total += 1
    return Result(name="webhooks", total=total)
