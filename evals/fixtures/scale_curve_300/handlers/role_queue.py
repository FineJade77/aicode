"""Role Queue handler."""

from contracts import Result


def handle(records):
    """Summarise role_queue records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "role_queue":
            continue
        total += 1
    return Result(name="role_queue", total=total)
