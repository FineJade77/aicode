"""File-backed storage."""

from pathlib import Path


class FileStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.txt"

    def put(self, key: str, value: str) -> None:
        self._path(key).write_text(value, encoding="utf-8")

    def get(self, key: str) -> str | None:
        path = self._path(key)
        return path.read_text(encoding="utf-8") if path.exists() else None
