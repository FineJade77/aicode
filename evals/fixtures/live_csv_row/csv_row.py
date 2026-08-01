"""Row splitting for a minimal CSV dialect.

A trailing separator means a trailing empty field. An earlier version of this
function dropped it, which silently shifted every column of the last row.
"""


def split_row(raw: str) -> list[str]:
    if raw == "":
        return [""]
    return raw.split(",")
