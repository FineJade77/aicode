"""Aggregate handler results."""

import importlib

from registry import HANDLERS


def summarise(records):
    total = 0
    for name in HANDLERS:
        module = importlib.import_module(f"handlers.{name}")
        total += module.handle(records).total
    return {"handlers": len(HANDLERS), "processed": total}
