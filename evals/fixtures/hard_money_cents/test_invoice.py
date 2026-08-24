from cart import Cart
from invoice import render


def test_a_half_cent_rounds_away_from_zero():
    cart = Cart()
    cart.add_line(0.125, 1)

    assert render(cart, 0.0)["subtotal"] == "0.13"


def test_rounding_happens_once_not_per_line():
    """Three lines of 0.125 sum to 0.375, which renders as 0.38.

    Rounding each line first gives 0.39 — the error the docstring warns about.
    """
    cart = Cart()
    for _ in range(3):
        cart.add_line(0.125, 1)

    assert render(cart, 0.0)["subtotal"] == "0.38"


def test_tax_uses_the_same_rule():
    cart = Cart()
    cart.add_line(1.00, 1)

    invoice = render(cart, 0.075)
    assert invoice["tax"] == "0.08"
    assert invoice["total"] == "1.08"
