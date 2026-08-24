"""Ledger Sync handler."""

from contracts import Result


def handle(records):
    """Summarise ledger_sync records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "ledger_sync":
            continue
        total += 1
    return Result(name="ledger_sync", total=total)
