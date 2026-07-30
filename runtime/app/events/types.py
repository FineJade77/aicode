from __future__ import annotations

from collections.abc import Mapping
from typing import Any

EVENT_TYPES = frozenset(
    {
        "session.created",
        "run.queued",
        "run.started",
        "run.steer.queued",
        "run.steer.applied",
        "run.cancelled",
        "run.budget.exceeded",
        "assistant.delta",
        "tool.started",
        "tool.output",
        "tool.denied",
        "tool.error",
        "tool.rejected",
        "approval.requested",
        "approval.expired",
        "edit.applied",
        "edit.rejected",
        "edit.auto_approved",
        "usage.recorded",
        "context.budget",
        "error",
        "final",
    }
)


def validate_event(event: Mapping[str, Any]) -> str:
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("runtime event must include a non-empty string type")
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown runtime event type: {event_type}")
    return event_type
