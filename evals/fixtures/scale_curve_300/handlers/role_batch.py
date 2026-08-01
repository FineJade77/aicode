"""Role Batch handler."""

from contracts import Result


def handle(records):
    """Summarise role_batch records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "role_batch":
            continue
        total += 1
    return Result(name="role_batch", total=total)
