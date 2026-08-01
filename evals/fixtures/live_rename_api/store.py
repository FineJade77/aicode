"""Record storage."""

RECORDS = [
    {"id": 1, "label": "alpha"},
    {"id": 2, "label": "beta"},
]


def fetch_all() -> list[dict]:
    """Return every stored record."""
    return list(RECORDS)
