"""Per-kind pricing."""

from kinds import KINDS

SIGNS = {"order": 1, "refund": -1}


def sign_for(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    return SIGNS[kind]
