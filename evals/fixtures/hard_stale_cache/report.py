"""Per-currency price report."""

from pricing import convert

CURRENCIES = ("USD", "EUR", "GBP")


def price_table(amount, base="USD"):
    return {currency: convert(amount, base, currency) for currency in CURRENCIES}
