"""In-memory session repository used by tests and embedded runtimes."""

from __future__ import annotations

from app.agent.ports import Clock, IdGenerator
from app.sessions.store import Session
from app.system import SystemClock, UuidGenerator


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

    def fork(self, session_id: str, *, message_id: int | None = None) -> Session:
        """Branch a session at a message.

        Message ids here are positions rather than database rows, so the mapping
        the SQLite store has to do is not needed — but the same refusal is: a
        cutoff that is not one of this session's messages would silently produce
        different history than was asked for.
        """
        source = self._sessions.get(session_id)
        if source is None:
            raise ValueError(f"session not found: {session_id}")
        if message_id is not None and message_id not in source.message_ids:
            raise ValueError(f"message {message_id} does not belong to session {session_id}")
        cutoff = message_id if message_id is not None else (source.message_ids[-1] if source.message_ids else 0)

        forked = self.create(source.workspace)
        for old_id, message in zip(source.message_ids, source.messages, strict=False):
            if old_id > cutoff:
                break
            forked.messages.append(dict(message))
            forked.message_ids.append(old_id)
        if source.plan:
            forked.set_plan(list(source.plan))
        return forked

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
