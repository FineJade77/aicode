from __future__ import annotations

from typing import Any

from app.security.secrets import redact_known_environment_secrets

SENSITIVE_KEY_FRAGMENTS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "private_key",
    "secret",
    "token",
}

SAFE_TOKEN_KEYS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
}

MAX_STRING_LENGTH = 1000


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_value_for_key(key, child) for key, child in value.items()}
    if isinstance(value, list):
        return [redact(child) for child in value]
    if isinstance(value, tuple):
        return [redact(child) for child in value]
    if isinstance(value, str):
        return truncate(value)
    return value


def redact_value_for_key(key: str, value: Any) -> Any:
    lowered = key.lower()
    if lowered in SAFE_TOKEN_KEYS:
        return redact(value)
    if any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS):
        return "[REDACTED]"
    return redact(value)


def truncate(value: str) -> str:
    value = redact_known_environment_secrets(value)
    if len(value) <= MAX_STRING_LENGTH:
        return value
    return value[:MAX_STRING_LENGTH] + "...[TRUNCATED]"
