"""Account Hook handler."""

from contracts import Result


def handle(records):
    """Summarise account_hook records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "account_hook":
            continue
        total += 1
    return Result(name="account_hook", total=total)
