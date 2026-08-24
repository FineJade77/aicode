"""Tariff Store handler."""

from contracts import Result


def handle(records):
    """Summarise tariff_store records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "tariff_store":
            continue
        total += 1
    return Result(name="tariff_store", total=total)
