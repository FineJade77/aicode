"""Security helpers for secrets, sensitive values, and protected paths."""

from __future__ import annotations

import hashlib
import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

SENSITIVE_ENV_FRAGMENTS = {
    "ACCESS_KEY",
    "API_KEY",
    "APIKEY",
    "ASKPASS",
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "PASSWORD",
    "PASSPHRASE",
    "PRIVATE",
    "SECRET",
    "SSH_",
    "TOKEN",
}
MIN_SECRET_LENGTH = 6


def stable_hash(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def is_protected_path(rel_path: str, protected_paths: list[str]) -> bool:
    normalized = rel_path.replace("\\", "/").lstrip("./")
    for pattern in protected_paths:
        normalized_pattern = pattern.replace("\\", "/").lstrip("./")
        if fnmatch(normalized, normalized_pattern):
            return True
        if "/" not in normalized_pattern and Path(normalized).name == normalized_pattern:
            return True
    return False


def sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return any(fragment in upper for fragment in SENSITIVE_ENV_FRAGMENTS)


def known_environment_secrets() -> tuple[str, ...]:
    values = {
        value
        for key, value in os.environ.items()
        if sensitive_env_key(key) and len(value) >= MIN_SECRET_LENGTH
    }
    return tuple(sorted(values, key=len, reverse=True))


def contains_known_environment_secret(value: str) -> bool:
    return any(secret in value for secret in known_environment_secrets())


def redact_known_environment_secrets(value: Any) -> Any:
    secrets = known_environment_secrets()
    return _redact(value, secrets)


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {key: _redact(child, secrets) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child, secrets) for child in value]
    if isinstance(value, tuple):
        return tuple(_redact(child, secrets) for child in value)
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
    return value
