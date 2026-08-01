"""Parse human-written amounts."""


def parse_amount(raw: str) -> int:
    """Parse an integer amount that may carry thousands separators, e.g. '1,234'."""
    return int(raw.strip())
