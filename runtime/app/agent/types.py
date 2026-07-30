from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.agent.ports import (
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
    """What Agent Core reads off a turn request.

    A structural type rather than a concrete class so the Application layer's
    `TurnRequest` and the eval runner's own request object both satisfy it
    without Agent Core importing either.
    """

    message: str
    mode: str
    workspace: str
    model: str | None


@dataclass(slots=True)
class AgentRuntime:
    """Agent Core's dependencies, named after the ports they satisfy.

    Fields are Optional because two consumers with different requirements share
    this object: `run_turn` needs the full set, while `ContextManager` works with
    only a session and tolerates a missing `model_runtime` by falling back to a
    deterministic summary. `run_turn` validates what it needs up front.
    """

    model_runtime: ModelRuntime | None
    trace: TraceSink | None
    policy: Any = None
    execution: ExecutionRuntime | None = None
    trust: ProjectTrustRepository | None = None
    tools: ToolRegistry | None = None
    workspace: WorkspaceRuntime | None = None
    clock: Clock | None = None
    approvals: ApprovalBroker | None = None
    context_manager: Any = None
