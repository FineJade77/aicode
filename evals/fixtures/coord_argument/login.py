"""Authentication events."""

from audit import record


def failed_login(user):
    return record("login.failed", user)
