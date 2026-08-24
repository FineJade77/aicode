"""Tax Store handler."""

from contracts import Result


def handle(records):
    """Summarise tax_store records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "tax_store":
            continue
        total += 1
    return Result(name="tax_store", total=total)
