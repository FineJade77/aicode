"""Role Cache handler."""

from contracts import Result


def handle(records):
    """Summarise role_cache records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "role_cache":
            continue
        total += 1
    return Result(name="role_cache", total=total)
