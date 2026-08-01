"""Configuration loading."""


def load(raw: dict) -> dict:
    if "name" not in raw:
        raise KeyError("name")
    return raw
