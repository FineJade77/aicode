"""Configuration validation."""


def validate(config: dict) -> dict:
    if config.get("retries", 0) < 0:
        raise ValueError("retries must not be negative")
    return config
