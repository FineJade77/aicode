"""Invoice rendering."""

import money


def render(cart, tax_rate):
    subtotal = cart.subtotal()
    tax = subtotal * tax_rate
    return {
        "subtotal": money.format_amount(subtotal),
        "tax": money.format_amount(tax),
        "total": money.format_amount(subtotal + tax),
    }
