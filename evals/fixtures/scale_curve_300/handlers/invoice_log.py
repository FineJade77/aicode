"""Invoice Log handler."""

from contracts import Result


def handle(records):
    """Summarise invoice_log records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "invoice_log":
            continue
        total += 1
    return Result(name="invoice_log", total=total)
