"""Role Log handler."""

from contracts import Result


def handle(records):
    """Summarise role_log records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "role_log":
            continue
        total += 1
    return Result(name="role_log", total=total)
