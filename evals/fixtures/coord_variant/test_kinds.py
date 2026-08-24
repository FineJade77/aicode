import pytest

from labels import label_for
from pricing import sign_for
from routing import queue_for


def test_the_existing_kinds_are_unchanged():
    assert (sign_for("order"), label_for("order"), queue_for("order")) == (1, "Order", "billing")
    assert (sign_for("refund"), label_for("refund"), queue_for("refund")) == (-1, "Refund", "billing-reversals")


def test_a_chargeback_is_priced_like_a_refund():
    assert sign_for("chargeback") == -1


def test_a_chargeback_has_its_own_label():
    assert label_for("chargeback") == "Chargeback"


def test_a_chargeback_routes_to_disputes():
    assert queue_for("chargeback") == "disputes"


def test_an_unknown_kind_is_still_rejected():
    for call in (sign_for, label_for, queue_for):
        with pytest.raises(ValueError):
            call("mystery")
