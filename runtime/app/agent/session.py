"""Session contracts and value types used by the agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

COMPACTION_SCHEMA_VERSION = 1

PlanItemStatus = Literal["pending", "in_progress", "done"]
PLAN_ITEM_STATUSES: frozenset[str] = frozenset(("pending", "in_progress", "done"))
MAX_PLAN_ITEMS = 40


@dataclass(frozen=True, slots=True)
class PlanItem:
    """One step of the model's externalised plan.

    Deliberately minimal: the more state a plan item carries, the more ways a
    model has to use it wrongly. Text plus a three-value status is enough for
    both purposes the plan serves — showing the user what the agent thinks it is
    doing, and giving the model something to check itself against.
    """

    text: str
    status: PlanItemStatus = "pending"

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "status": self.status}
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0


class ApprovalDecision(StrEnum):
    """Why an approval request stopped waiting.

    A tri-state bool previously collapsed "the user said no" and "nobody
    answered in time" into the same `False`. The model then saw
    "user rejected this edit" for an unattended request and could plausibly
    abandon a correct plan, so the two outcomes are now distinct.
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    MISSING = "missing"


class EventSink(Protocol):
    async def put(self, event: dict[str, Any]) -> None: ...

    def events_after(self, after: int | None = None) -> list[dict[str, Any]]: ...

    def last_event_id(self) -> int: ...

    def set_default_after(self, after: int | None) -> None: ...

    def default_after(self) -> int | None: ...

    def set_default_run_id(self, run_id: str | None) -> None: ...

    def default_run_id(self) -> str | None: ...

    def set_current_run_id(self, run_id: str | None) -> None: ...

    def subscribe(self, after: int | None = None, *, idle_timeout: float | None = None) -> Any: ...

    def final_event_for_run(self, run_id: str) -> dict[str, Any] | None: ...

    def has_events_for_run(self, run_id: str) -> bool: ...


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
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

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
    events: EventSink
    messages: list[dict[str, Any]]
    message_ids: list[int]
    compactions: list[CompactionEntry]
    approvals: dict[str, Any]
    agent_queue: Any
    steer_queue: Any
    agent_runner_task: Any
    current_run_id: str | None
    current_run_stage: str | None
    auto_accept_edits: bool
    plan: list[PlanItem]
    read_files: dict[str, str]

    def to_dict(self) -> dict[str, Any]: ...

    def enqueue_agent_run(self, request: Any) -> Any: ...

    def queued_run_ids(self) -> set[str]: ...

    def enqueue_steer(self, message: str) -> int: ...

    def drain_steers(self) -> list[str]: ...

    def next_agent_run(self) -> Any | None: ...

    def start_agent_run(self, run_id: str) -> None: ...

    def finish_agent_run(self) -> None: ...

    def agent_runner_active(self) -> bool: ...

    def append_message(self, message: dict[str, Any]) -> int | None: ...

    def set_plan(self, items: list[PlanItem]) -> list[PlanItem]: ...

    def record_read(self, path: str, content_hash: str) -> None: ...

    def read_hash(self, path: str) -> str | None: ...

    def forget_read(self, path: str) -> None: ...

    def append_compaction(self, entry: CompactionEntry) -> CompactionEntry: ...

    def mark_agent_progress(self, stage: str) -> None: ...

    def create_approval(self, kind: str, payload: dict[str, Any]) -> Any: ...

    def resolve_approval(self, approval_id: str, accepted: bool) -> bool: ...

    def expire_pending_approvals(self) -> list[Any]: ...

    async def wait_for_approval(
        self,
        approval_id: str,
        timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ) -> ApprovalDecision: ...
