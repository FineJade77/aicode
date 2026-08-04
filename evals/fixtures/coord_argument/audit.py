"""Audit record construction."""


def record(action, detail):
    """Build one audit entry."""
    return {"action": action, "detail": detail}
