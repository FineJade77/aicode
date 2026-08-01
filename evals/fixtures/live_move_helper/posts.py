"""Blog posts."""


def slugify(title: str) -> str:
    """Turn a title into a URL slug."""
    return "-".join(part.lower() for part in title.split() if part)


def permalink(title: str) -> str:
    return f"/posts/{slugify(title)}"
