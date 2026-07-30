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

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[dict]:
        ordered = sorted(self._sessions.values(), key=lambda item: item.updated_at, reverse=True)
        window = ordered[max(0, offset) :]
        if limit is not None:
            window = window[: max(0, limit)]
        # Summaries only, matching SessionStore.list(): a listing must not carry
        # every message of every session.
        return [
            {
                "session_id": session.session_id,
                "workspace": session.workspace,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "message_count": len(session.messages),
                "agent": session.agent_state(),
            }
            for session in window
        ]

    def prune(self, *, max_sessions: int | None = None, max_age_days: int | None = None) -> dict:
        """Retention is a no-op for an ephemeral repository.

        Nothing is persisted, so there is nothing to reclaim; reporting
        "disabled" keeps the port satisfied without pretending to delete.
        """
        del max_sessions, max_age_days
        return {"status": "disabled", "deleted_sessions": 0, "deleted_messages": 0, "retained_live": 0}

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
