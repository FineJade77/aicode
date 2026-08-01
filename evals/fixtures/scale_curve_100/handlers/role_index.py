"""Role Index handler."""

from contracts import Result


def handle(records):
    """Summarise role_index records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "role_index":
            continue
        total += 1
    return Result(name="role_index", total=total)
