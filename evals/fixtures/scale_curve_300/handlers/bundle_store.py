"""Bundle Store handler."""

from contracts import Result


def handle(records):
    """Summarise bundle_store records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "bundle_store":
            continue
        total += 1
    return Result(name="bundle_store", total=total)
