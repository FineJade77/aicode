from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.loop import AgentLoop
from app.agent.types import AgentRuntime
from app.application.services import (
    ApprovalService,
    ExecutionApplicationService,
    ModelService,
    ProjectTrustService,
    RunCoordinator,
    SandboxLimits,
    SessionService,
    TraceService,
)
from app.contracts.api import contract_descriptor
from app.core.ports import (
    Clock,
    ExecutionRuntime,
    ModelRuntime,
    ProjectTrustRepository,
    SessionRepository,
    TraceSink,
    UsageRuntime,
    WorkspaceRuntime,
)


@dataclass(slots=True)
class ApplicationRuntime:
    """Composition root result consumed by transport adapters."""

    settings: Any
    sessions: SessionRepository
    model: ModelRuntime
    trace: TraceSink
    execution: ExecutionRuntime
    trust: ProjectTrustRepository
    workspace: WorkspaceRuntime
    usage: UsageRuntime
    clock: Clock
    agent: AgentRuntime
    sandbox_limits: SandboxLimits
    session_service: SessionService = field(init=False)
    runs: RunCoordinator = field(init=False)
    approvals: ApprovalService = field(init=False)
    traces: TraceService = field(init=False)
    projects: ProjectTrustService = field(init=False)
    executions: ExecutionApplicationService = field(init=False)
    models: ModelService = field(init=False)

    def __post_init__(self) -> None:
        loop = AgentLoop(self.agent)
        self.session_service = SessionService(self.sessions, self.trace, self.workspace)
        self.runs = RunCoordinator(self.model, self.trace, loop)
        self.approvals = ApprovalService(self.trace)
        self.traces = TraceService(self.trace, self.usage, self.clock)
        self.projects = ProjectTrustService(self.trust, self.trace)
        self.executions = ExecutionApplicationService(self.execution, self.workspace, self.sandbox_limits)
        self.models = ModelService(self.model)

    def status(self, *, pid: int) -> dict[str, Any]:
        return {
            "status": "ok",
            "name": self.settings.app_name,
            "version": self.settings.version,
            "pid": pid,
            "contract": contract_descriptor(self.settings.version),
            "audit_writer": self.trace.status(),
            "event_writer": self.sessions.event_writer_status(),
            "executions": self.execution.status(),
        }

    async def prepare_stop(self) -> dict[str, Any]:
        await self.execution.cancel_all()
        return {"status": "ready", "executions": self.execution.status()}

    async def aclose(self) -> None:
        await self.execution.cancel_all()
        await self.model.aclose()
        await self.trace.aclose()
        await self.sessions.aclose()
