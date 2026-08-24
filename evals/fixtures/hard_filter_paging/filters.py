"""Row filters."""


def is_visible(row):
    return not row.get("hidden", False)
