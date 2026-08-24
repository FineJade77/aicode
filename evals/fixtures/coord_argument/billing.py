"""Billing events."""

from audit import record


def charge_declined(invoice):
    return record("billing.declined", invoice)
