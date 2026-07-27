from __future__ import annotations

import time
from datetime import date, datetime, timezone
from uuid import uuid4


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> date:
        return self.now().date()

    def monotonic(self) -> float:
        return time.monotonic()


class UuidGenerator:
    def new(self, prefix: str) -> str:
        return f"{prefix}_{uuid4().hex[:12]}"
