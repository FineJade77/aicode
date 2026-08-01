"""The service the configuration drives."""

from config import KNOWN_KEYS, Config


def configure(config: Config, overrides: dict) -> dict:
    """Apply `overrides` on top of `config`, ignoring keys we do not know."""
    applied = {"host": config.host, "port": config.port}
    for key, value in overrides.items():
        if key in KNOWN_KEYS:
            applied[key] = value
    return applied
