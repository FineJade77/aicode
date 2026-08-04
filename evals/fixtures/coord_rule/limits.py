"""Shared submission limit."""

MAX_LENGTH = 200


def check_length(text):
    if len(text) > MAX_LENGTH:
        raise ValueError("too long")
    return text
