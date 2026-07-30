"""Session-backed approval handling for agent tool calls."""

from __future__ import annotations

import os
from typing import Any

from app.agent.session import DEFAULT_APPROVAL_TIMEOUT_SECONDS, AgentSession, ApprovalDecision


class SessionApprovalBroker:
    """Bridge Agent Core approval requests to persistent session state/events."""

    def __init__(self, *, timeout_seconds: float | None = None) -> None:
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else default_approval_timeout_seconds()
        )

    async def request(
        self,
        session: AgentSession,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> ApprovalDecision:
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
        decision = await session.wait_for_approval(
            approval.approval_id,
            timeout_seconds=self.timeout_seconds,
        )
        if decision is ApprovalDecision.TIMED_OUT:
            # Reuses approval.expired with an explicit reason rather than adding a
            # near-duplicate event type; run cancellation reports the same event
            # with reason="run_cancelled".
            await session.events.put(
                {
                    "type": "approval.expired",
                    "approval_id": approval.approval_id,
                    "kind": kind,
                    "reason": "timeout",
                    "timeout_seconds": self.timeout_seconds,
                    "message": (
                        f"No decision was recorded within {self.timeout_seconds:g}s, "
                        "so the request was not approved."
                    ),
                }
            )
        return decision


def default_approval_timeout_seconds() -> float:
    try:
        value = float(os.getenv("AICODE_APPROVAL_TIMEOUT_SECONDS", str(DEFAULT_APPROVAL_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_APPROVAL_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_APPROVAL_TIMEOUT_SECONDS
