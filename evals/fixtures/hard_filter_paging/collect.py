"""Walk every page and gather the rows."""

from page import fetch_page


def collect_all(rows, size=2):
    gathered = []
    cursor = 0
    while cursor is not None:
        page, cursor = fetch_page(rows, cursor, size)
        if not page:
            break
        gathered.extend(page)
    return gathered
