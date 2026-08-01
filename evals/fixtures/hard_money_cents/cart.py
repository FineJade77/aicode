"""Shopping cart totals."""

import money


class Cart:
    def __init__(self):
        self.lines = []

    def add_line(self, price, quantity):
        self.lines.append((price, quantity))

    def subtotal(self):
        total = 0
        for price, quantity in self.lines:
            total += money.round_cents(price * quantity)
        return total
