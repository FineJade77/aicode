"""Export Hook handler."""

from contracts import Result


def handle(records):
    """Summarise export_hook records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "export_hook":
            continue
        total += 1
    return Result(name="export_hook", total=total)
