"""Deadline comparison."""

from datetime import datetime, timezone


def is_overdue(deadline: datetime, now: datetime | None = None) -> bool:
    """Whether `deadline` has passed.

    `deadline` may be naive, in which case it is meant as UTC.
    """
    now = now or datetime.now(timezone.utc)
    return deadline < now
