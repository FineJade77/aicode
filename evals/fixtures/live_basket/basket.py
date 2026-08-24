"""Shopping basket helpers."""


def add_item(name: str, items: list | None = []) -> list:
    """Return a basket with `name` appended. Callers may pass their own list."""
    items.append(name)
    return items
