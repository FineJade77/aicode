from __future__ import annotations

from app.adapters.system import SystemClock, UuidGenerator
from app.core.ports import Clock, IdGenerator
from app.sessions.store import Session


class InMemorySessionRepository:
    """Ephemeral session adapter for embedding, tests and SDK callers."""

    def __init__(self, *, clock: Clock | None = None, ids: IdGenerator | None = None) -> None:
        self.clock = clock or SystemClock()
        self.ids = ids or UuidGenerator()
        self._sessions: dict[str, Session] = {}
        self._last_session_id: str | None = None

    def create(self, workspace: str) -> Session:
        now = self.clock.now()
        session = Session(
            session_id=self.ids.new("sess"),
            workspace=workspace,
            created_at=now,
            updated_at=now,
            clock=self.clock,
            ids=self.ids,
        )
        self._sessions[session.session_id] = session
        self._last_session_id = session.session_id
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list(self) -> list[dict]:
        return [
            session.to_dict()
            for session in sorted(
                self._sessions.values(),
                key=lambda item: item.updated_at,
                reverse=True,
            )
        ]

    def last(self) -> Session | None:
        return self.get(self._last_session_id) if self._last_session_id else None

    def event_writer_status(self) -> dict[str, int | bool]:
        return {
            "active": False,
            "queue_size": 0,
            "queue_capacity": 0,
            "enqueued": 0,
            "written": 0,
            "dropped": 0,
            "failed": 0,
        }

    async def flush(self) -> None:
        return None

    async def aclose(self) -> None:
        return None
