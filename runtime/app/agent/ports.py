"""Dependency interfaces consumed by the agent and application services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from app.agent.session import AgentSession
from app.models.provider import CompletionResult, ModelCapability

ApprovalMode = Literal["none", "gate", "diff"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Metadata shared by providers, policy decisions, and tool execution."""

    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    approval: ApprovalMode = "gate"
    hidden_in_modes: frozenset[str] = field(default_factory=frozenset)

    def to_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    def visible_in(self, mode: str) -> bool:
        return mode not in self.hidden_in_modes


@runtime_checkable
class ModelRuntime(Protocol):
    primary: Any
    settings: Any

    def capability_for_purpose(self, purpose: str) -> ModelCapability: ...

    def capability_for_model(self, model: str) -> ModelCapability: ...

    async def stream_complete(
        self,
        *,
        purpose: str,
        model: str | None = None,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | tuple[Any, ...] = (),
        on_text_delta: Any = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> CompletionResult: ...

    async def probe(self, *, model: str | None = None, tools: bool = True) -> dict[str, Any]: ...

    async def probe_routes(self) -> dict[str, Any]: ...

    def route_status(self) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class TraceSink(Protocol):
    path: Path

    def record(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        workspace: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None: ...

    def status(self) -> dict[str, Any]: ...

    async def flush(self) -> None: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class SessionRepository(Protocol):
    def create(self, workspace: str) -> AgentSession: ...

    def get(self, session_id: str) -> AgentSession | None: ...

    def fork(self, session_id: str, *, message_id: int | None = None) -> AgentSession: ...

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]: ...

    def prune(self, *, max_sessions: int | None = None, max_age_days: int | None = None) -> dict[str, Any]: ...

    def last(self) -> AgentSession | None: ...

    def event_writer_status(self) -> dict[str, Any]: ...

    async def flush(self) -> None: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class ProjectTrustRepository(Protocol):
    def status(self, workspace: Path) -> dict[str, Any]: ...

    def trust(self, workspace: Path) -> dict[str, Any]: ...

    def remove(self, workspace: Path) -> bool: ...

    def list(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class ExecutionRuntime(Protocol):
    async def execute(self, request: Any) -> Any: ...

    async def cancel(self, execution_id: str) -> bool: ...

    async def cancel_all(self) -> None: ...

    def status(self) -> dict[str, Any]: ...


@runtime_checkable
class ToolRuntime(Protocol):
    def schemas_for_mode(self, mode: str) -> list[dict[str, Any]]: ...

    def build_context(
        self,
        workspace: str,
        mode: str,
        *,
        execution: Any = None,
        session_id: str = "",
        run_id: str = "",
        trust_level: str = "trusted",
        session: Any = None,
    ) -> Any: ...

    def spec_for(self, name: str) -> ToolSpec | None: ...

    def specs(self) -> list[ToolSpec]: ...

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> str | None: ...

    async def run(self, name: str, arguments: dict[str, Any], context: Any) -> Any: ...

    def build_edit_proposal(self, context: Any, arguments: dict[str, Any]) -> Any: ...

    def apply_edit(self, workspace: Path, proposal: Any) -> None: ...

    def is_protected_path(self, path: str, protected_paths: list[str]) -> bool: ...


class ToolRegistry(ToolRuntime, Protocol):
    """Named Agent Core port; ToolRuntime is retained as a compatibility alias."""


@runtime_checkable
class WorkspaceRuntime(Protocol):
    def same_workspace(self, left: str, right: str) -> bool: ...

    def prompt_context(self, workspace: Path, *, trust_level: str = "trusted") -> Any: ...

    def project_command(self, workspace: Path, action: str) -> str | None: ...

    def review_rules(self, workspace: str | None = None) -> dict[str, Any]: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...

    def today(self) -> date: ...

    def monotonic(self) -> float: ...


@runtime_checkable
class IdGenerator(Protocol):
    def new(self, prefix: str) -> str: ...


@runtime_checkable
class UsageRuntime(Protocol):
    def summarize(self, *, session_id: str | None = None, day: date | None = None) -> dict[str, Any]: ...


@runtime_checkable
class ApprovalBroker(Protocol):
    async def request(
        self,
        session: AgentSession,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> bool | None: ...
