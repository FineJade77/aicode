"""Row rendering."""

SEPARATOR = "\t"


def format_row(cells: list[str]) -> str:
    return SEPARATOR.join(cells)
