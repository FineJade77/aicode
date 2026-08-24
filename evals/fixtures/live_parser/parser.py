"""Key=value parsing."""


def parse_pair(raw: str) -> tuple[str, str]:
    """Parse 'key=value'.

    Raises ValueError when there is no '=' or when the key is empty.
    """
    if "=" not in raw:
        raise ValueError(f"missing '=': {raw!r}")
    key, _, value = raw.partition("=")
    if not key.strip():
        raise ValueError(f"empty key: {raw!r}")
    return key.strip(), value.strip()
