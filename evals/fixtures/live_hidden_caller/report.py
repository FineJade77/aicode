"""Report built on the row formatter."""

from row_format import format_row


def build_report(rows: list[list[str]]) -> str:
    return "\n".join(format_row(row) for row in rows)


def column_count(rendered_row: str) -> int:
    """How many columns a rendered row has."""
    return len(rendered_row.split("\t"))
