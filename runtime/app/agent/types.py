from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.core.ports import (
    ApprovalBroker,
    Clock,
    ExecutionRuntime,
    ModelRuntime,
    ProjectTrustRepository,
    ToolRegistry,
    TraceSink,
    WorkspaceRuntime,
)


class AgentRequest(Protocol):
    message: str
    mode: str
    workspace: str
    language: str
    model: str | None


@dataclass(slots=True)
class AgentRuntime:
    model_router: ModelRuntime | None
    audit: TraceSink | None
    policy: Any = None
    execution: ExecutionRuntime | None = None
    trust_store: ProjectTrustRepository | None = None
    tools: ToolRegistry | None = None
    workspace: WorkspaceRuntime | None = None
    clock: Clock | None = None
    approvals: ApprovalBroker | None = None
    context_manager: Any = None

    @property
    def model_runtime(self) -> ModelRuntime | None:
        """Port-oriented alias kept alongside the legacy field name."""
        return self.model_router

    @property
    def trace(self) -> TraceSink | None:
        return self.audit

    @property
    def trust(self) -> ProjectTrustRepository | None:
        return self.trust_store
