"""Prices converted into a requested currency."""

from rates import rate_for


def convert(amount, base, currency):
    return round(amount * rate_for(base, currency=currency), 4)
