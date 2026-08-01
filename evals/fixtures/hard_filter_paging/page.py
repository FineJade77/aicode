"""Cursor pagination over a filtered list.

`fetch_page` returns at most `size` visible rows plus the cursor to pass in for
the next page. `None` as the next cursor means the end of the data.
"""

from filters import is_visible


def fetch_page(rows, cursor=0, size=2):
    window = rows[cursor : cursor + size]
    visible = [row for row in window if is_visible(row)]
    next_cursor = cursor + size
    if next_cursor >= len(rows):
        next_cursor = None
    return visible, next_cursor
