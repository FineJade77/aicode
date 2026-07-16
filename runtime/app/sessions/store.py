from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    kind: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decision_event: asyncio.Event = field(default_factory=asyncio.Event)
    accepted: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "kind": self.kind,
            "status": "pending" if self.accepted is None else ("accepted" if self.accepted else "rejected"),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(slots=True)
class Session:
    session_id: str
    workspace: str
    language: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    events: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    messages: list[dict[str, Any]] = field(default_factory=list)
    approvals: dict[str, PendingApproval] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "language": self.language,
            "created_at": self.created_at.isoformat(),
            "messages": self.messages,
            "approvals": [approval.to_dict() for approval in self.approvals.values()],
        }

    def create_approval(self, kind: str, payload: dict[str, Any]) -> PendingApproval:
        approval = PendingApproval(
            approval_id=f"appr_{uuid4().hex[:12]}",
            kind=kind,
            payload=payload,
        )
        self.approvals[approval.approval_id] = approval
        return approval

    def resolve_approval(self, approval_id: str, accepted: bool) -> bool:
        approval = self.approvals.get(approval_id)
        if approval is None or approval.accepted is not None:
            return False
        approval.accepted = accepted
        approval.decision_event.set()
        return True

    async def wait_for_approval(self, approval_id: str, timeout_seconds: float = 300.0) -> bool | None:
        approval = self.approvals.get(approval_id)
        if approval is None:
            return None
        try:
            await asyncio.wait_for(approval.decision_event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return None
        return approval.accepted


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
