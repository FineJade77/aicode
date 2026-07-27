from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


COMPACTION_SCHEMA_VERSION = 1


class EventSink(Protocol):
    async def put(self, event: dict[str, Any]) -> None: ...

    def events_after(self, after: int | None = None) -> list[dict[str, Any]]: ...

    def last_event_id(self) -> int: ...

    def set_default_after(self, after: int | None) -> None: ...

    def default_after(self) -> int | None: ...

    def set_default_run_id(self, run_id: str | None) -> None: ...

    def default_run_id(self) -> str | None: ...

    def set_current_run_id(self, run_id: str | None) -> None: ...

    def subscribe(self, after: int | None = None) -> Any: ...


@dataclass(frozen=True, slots=True)
class CompactionEntry:
    compaction_id: int | None
    session_id: str
    schema_version: int
    start_message_id: int
    end_message_id: int
    summary: str
    provider: str
    model: str
    prompt_version: str
    before_tokens: int
    after_tokens: int
    context_window: int
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "compaction_id": self.compaction_id,
            "session_id": self.session_id,
            "schema_version": self.schema_version,
            "start_message_id": self.start_message_id,
            "end_message_id": self.end_message_id,
            "summary": self.summary,
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "context_window": self.context_window,
            "created_at": self.created_at.isoformat(),
        }


class AgentSession(Protocol):
    session_id: str
    workspace: str
    language: str
    events: EventSink
    messages: list[dict[str, Any]]
    message_ids: list[int]
    compactions: list[CompactionEntry]
    approvals: dict[str, Any]
    agent_queue: Any
    agent_runner_task: Any
    current_run_id: str | None
    auto_accept_edits: bool

    def to_dict(self) -> dict[str, Any]: ...

    def enqueue_agent_run(self, request: Any) -> Any: ...

    def next_agent_run(self) -> Any | None: ...

    def start_agent_run(self, run_id: str) -> None: ...

    def finish_agent_run(self) -> None: ...

    def agent_runner_active(self) -> bool: ...

    def append_message(self, message: dict[str, Any]) -> int | None: ...

    def append_compaction(self, entry: CompactionEntry) -> CompactionEntry: ...

    def mark_agent_progress(self, stage: str) -> None: ...

    def create_approval(self, kind: str, payload: dict[str, Any]) -> Any: ...

    def resolve_approval(self, approval_id: str, accepted: bool) -> bool: ...

    def expire_pending_approvals(self) -> list[Any]: ...

    async def wait_for_approval(self, approval_id: str, timeout_seconds: float = 300.0) -> bool | None: ...
