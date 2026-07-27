from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from app.usage.store import summarize_usage


class JsonlUsageRuntime:
    """Read usage aggregates from the append-only trace JSONL adapter."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def summarize(self, *, session_id: str | None = None, day: date | None = None) -> dict[str, Any]:
        return summarize_usage(self.path, session_id=session_id, day=day)
