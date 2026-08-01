"""Page a list for a 1-based UI."""


def paginate(items: list, page: int, per_page: int) -> list:
    """Return the slice of `items` shown on `page`, where the first page is 1."""
    start = page * per_page
    return items[start : start + per_page]
