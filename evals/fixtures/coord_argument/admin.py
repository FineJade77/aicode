"""Administrative events."""

from audit import record


def role_granted(target):
    return record("admin.role_granted", target)
