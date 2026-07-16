from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class Session:
    session_id: str
    workspace: str
    language: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    events: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    messages: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "language": self.language,
            "created_at": self.created_at.isoformat(),
            "messages": self.messages,
        }


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._last_session_id: str | None = None

    def create(self, workspace: str, language: str) -> Session:
        session = Session(
            session_id=f"sess_{uuid4().hex[:12]}",
            workspace=workspace,
            language=language,
        )
        self._sessions[session.session_id] = session
        self._last_session_id = session.session_id
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list(self) -> list[dict[str, Any]]:
        return [session.to_dict() for session in self._sessions.values()]

    def last(self) -> Session | None:
        if not self._last_session_id:
            return None
        return self.get(self._last_session_id)


store = SessionStore()
