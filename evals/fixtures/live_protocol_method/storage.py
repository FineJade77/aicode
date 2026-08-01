"""The storage interface every backend implements."""

from typing import Protocol


class Storage(Protocol):
    def put(self, key: str, value: str) -> None: ...

    def get(self, key: str) -> str | None: ...
