"""Returns settings."""

SECTION = "returns"

DEFAULTS = {
    "enabled": True,
    "retry_limit": 3,
    "timeout_seconds": 30,
}


def defaults():
    return dict(DEFAULTS)
