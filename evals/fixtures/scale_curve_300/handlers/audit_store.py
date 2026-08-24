"""Audit Store handler."""

from contracts import Result


def handle(records):
    """Summarise audit_store records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "audit_store":
            continue
        total += 1
    return Result(name="audit_store", total=total)
