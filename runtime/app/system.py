"""System clock and identifier implementations."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from uuid import uuid4


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def today(self) -> date:
        return self.now().date()

    def monotonic(self) -> float:
        return time.monotonic()


class UuidGenerator:
    def new(self, prefix: str) -> str:
        return f"{prefix}_{uuid4().hex[:12]}"
