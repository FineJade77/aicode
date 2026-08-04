"""Profile bio submission."""

from limits import check_length


def submit_bio(text):
    return check_length(text)
