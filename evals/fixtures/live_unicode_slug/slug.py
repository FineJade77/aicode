"""Slug generation that keeps non-ASCII letters intact."""

SEPARATORS = " \t-_"


def slugify(title: str) -> str:
    """Lower-case the title and join its words with hyphens.

    Non-ASCII letters are preserved rather than stripped, so titles in
    languages other than English keep their meaning.
    """
    words = title.lower()
    for separator in SEPARATORS:
        words = words.replace(separator, " ")
    return "-".join(part for part in words.split(" ") if part)
