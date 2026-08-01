"""Retry helper."""

from collections.abc import Callable
from typing import Any


def call_with_retries(operation: Callable[[], Any], attempts: int) -> Any:
    """Call `operation` until it succeeds, making at most `attempts` calls."""
    last: BaseException | None = None
    for _ in range(attempts - 1):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - recorded and re-raised below
            last = exc
    raise last if last is not None else RuntimeError("no attempt was made")
