"""Tariff Hook handler."""

from contracts import Result


def handle(records):
    """Summarise tariff_hook records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "tariff_hook":
            continue
        total += 1
    return Result(name="tariff_hook", total=total)
