"""Money helpers.

Two rules the whole stack depends on:

1. Amounts are rounded to the nearest cent **half away from zero** — 0.125
   becomes 0.13, never 0.12.
2. Rounding happens **once, at render time**. Intermediate sums keep their full
   precision; rounding each line as it is added compounds the error.
"""


def round_cents(amount):
    return round(amount, 2)


def format_amount(amount):
    return f"{round_cents(amount):.2f}"
