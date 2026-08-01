"""Coupons settings."""

SECTION = "coupons"

DEFAULTS = {
    "enabled": True,
    "retry_limit": 3,
    "timeout": 30,
}


def defaults():
    return dict(DEFAULTS)
