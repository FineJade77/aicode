"""In-memory storage."""


class MemoryStorage:
    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def put(self, key: str, value: str) -> None:
        self.items[key] = value

    def get(self, key: str) -> str | None:
        return self.items.get(key)
