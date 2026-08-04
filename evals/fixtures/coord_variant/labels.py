"""Per-kind display labels."""

from kinds import KINDS


def label_for(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    return "Order" if kind == "order" else "Refund"
