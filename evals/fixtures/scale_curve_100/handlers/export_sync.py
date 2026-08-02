"""Export Sync handler."""

from contracts import Result


def handle(records):
    """Summarise export_sync records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "invoice_queue":
            continue
        total += 1
    return Result(name="export_sync", total=total)
