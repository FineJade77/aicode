"""Warehouses settings."""

SECTION = "warehouses"

DEFAULTS = {
    "enabled": True,
    "retries": 3,
    "timeout_seconds": 30,
}


def defaults():
    return dict(DEFAULTS)
