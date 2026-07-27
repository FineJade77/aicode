from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.core.session import AgentSession
from app.models.provider import CompletionResult, ModelCapability


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
    def create(self, workspace: str, language: str) -> AgentSession: ...

    def get(self, session_id: str) -> AgentSession | None: ...

    def list(self) -> list[dict[str, Any]]: ...

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
        language: str,
        *,
        execution: Any = None,
        session_id: str = "",
        run_id: str = "",
        trust_level: str = "trusted",
    ) -> Any: ...

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> str | None: ...

    async def run(self, name: str, arguments: dict[str, Any], context: Any) -> Any: ...

    def build_edit_proposal(
        self,
        workspace: Path,
        arguments: dict[str, Any],
        protected_paths: list[str],
    ) -> Any: ...

    def apply_edit(self, workspace: Path, proposal: Any) -> None: ...

    def is_protected_path(self, path: str, protected_paths: list[str]) -> bool: ...


class ToolRegistry(ToolRuntime, Protocol):
    """Named Agent Core port; ToolRuntime is retained as a compatibility alias."""


@runtime_checkable
class WorkspaceRuntime(Protocol):
    def effective_language(self, workspace: str, requested_language: str) -> str: ...

    def same_workspace(self, left: str, right: str) -> bool: ...

    def prompt_context(self, workspace: Path) -> Any: ...

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
        language: str,
    ) -> bool | None: ...
