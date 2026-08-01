"""Role Feed handler."""

from contracts import Result


def handle(records):
    """Summarise role_feed records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "role_feed":
            continue
        total += 1
    return Result(name="role_feed", total=total)
