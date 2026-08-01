"""Account Queue handler."""

from contracts import Result


def handle(records):
    """Summarise account_queue records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "account_queue":
            continue
        total += 1
    return Result(name="account_queue", total=total)
