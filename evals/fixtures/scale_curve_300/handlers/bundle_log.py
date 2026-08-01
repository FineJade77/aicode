"""Bundle Log handler."""

from contracts import Result


def handle(records):
    """Summarise bundle_log records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "bundle_log":
            continue
        total += 1
    return Result(name="bundle_log", total=total)
