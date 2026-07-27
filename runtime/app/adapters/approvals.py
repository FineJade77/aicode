from __future__ import annotations

from typing import Any

from app.core.session import AgentSession


class SessionApprovalBroker:
    """Bridge Agent Core approval requests to persistent session state/events."""

    async def request(
        self,
        session: AgentSession,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> bool | None:
        approval = session.create_approval(kind, payload)
        session.mark_agent_progress(f"approval.{kind}")
        message = "waiting for user approval"
        await session.events.put(
            {
                "type": "approval.requested",
                "approval_id": approval.approval_id,
                "kind": kind,
                "message": message,
                **payload,
            }
        )
        return await session.wait_for_approval(approval.approval_id)
