from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.audit.redaction import redact


class AuditLogger:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def from_env(cls) -> "AuditLogger":
        explicit = os.getenv("AICODE_AUDIT_PATH")
        if explicit:
            return cls(Path(explicit))

        home = os.getenv("AICODE_HOME")
        if home:
            return cls(Path(home) / "audit.jsonl")

        return cls(Path.home() / ".aicode" / "audit.jsonl")

    def record(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        workspace: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "session_id": session_id,
            "workspace": workspace,
            "data": redact(data or {}),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def stable_hash(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()
