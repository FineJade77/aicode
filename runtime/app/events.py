"""Runtime event validation and SSE serialization."""

from __future__ import annotations

import json
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
        "run.verification.exhausted",
        "run.no_progress",
        "verify.attempt",
        "plan.updated",
        "mcp.server.started",
        "mcp.server.failed",
        "provider.fallback",
        "assistant.delta",
        "tool.started",
        "tool.output",
        "tool.denied",
        "tool.error",
        "tool.rejected",
        "approval.requested",
        "question.asked",
        "approval.expired",
        "edit.applied",
        "edit.rejected",
        "edit.auto_approved",
        "hook.finished",
        "hook.blocked",
        "usage.recorded",
        "context.budget",
        "error",
        "final",
    }
)


def encode_sse(event: dict[str, Any]) -> str:
    event_type = event.get("type", "message")
    payload = json.dumps(event, ensure_ascii=False)
    event_id = event.get("event_id")
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_line}event: {event_type}\ndata: {payload}\n\n"


def validate_event(event: Mapping[str, Any]) -> str:
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("runtime event must include a non-empty string type")
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown runtime event type: {event_type}")
    return event_type
