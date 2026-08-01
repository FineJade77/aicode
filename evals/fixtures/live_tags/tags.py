"""Tag lists that never hold duplicates."""


def add_tag(tags: list[str], tag: str) -> list[str]:
    """Return `tags` with `tag` appended, unless it is already present.

    Adding the same tag twice leaves the list unchanged, so callers can apply
    the same set of tags repeatedly without drift.
    """
    if tag in tags:
        return list(tags)
    return [*tags, tag]
