"""Per-kind queue routing."""

from kinds import KINDS


def queue_for(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    return "billing" if kind == "order" else "billing-reversals"
