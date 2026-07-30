from __future__ import annotations

from app.models.provider import CompletionRequest, StreamEvent, ToolCallRequest, Usage


class FakeProvider:
    provider_name = "fake"

    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self.turns = list(turns)
        self.calls: list[CompletionRequest] = []

    def is_configured(self) -> bool:
        return True

    async def stream_complete(self, request: CompletionRequest):
        self.calls.append(request)
        for event in self.turns.pop(0):
            yield event


def text_turn(text: str, input_tokens: int = 10, output_tokens: int = 5) -> list[StreamEvent]:
    return [
        StreamEvent(type="text_delta", text=text),
        StreamEvent(type="done", usage=Usage(input_tokens, output_tokens), model="fake-model"),
    ]


def tool_turn(name: str, arguments: dict, call_id: str = "tc_1", text: str = "") -> list[StreamEvent]:
    events = []
    if text:
        events.append(StreamEvent(type="text_delta", text=text))
    events.append(StreamEvent(type="tool_call", tool_call=ToolCallRequest(id=call_id, name=name, arguments=arguments)))
    events.append(StreamEvent(type="done", usage=Usage(10, 5), model="fake-model"))
    return events


def build_test_runtime(
    tmp_path,
    *,
    sessions=None,
    trace=None,
    model=None,
    execution=None,
    trust=None,
    workspace=None,
    tools=None,
    approvals=None,
    settings=None,
    agent=None,
):
    """Build a real ApplicationRuntime with selected components replaced.

    Handlers now receive the runtime as a dependency, so tests construct one
    instead of monkeypatching a module global. Components must be supplied up
    front rather than assigned afterwards: ApplicationRuntime wires its services
    in __post_init__, so a later attribute assignment would leave the services
    pointing at the original objects.
    """
    from pathlib import Path

    from app.adapters.approvals import SessionApprovalBroker
    from app.adapters.system import SystemClock
    from app.adapters.tools import DefaultToolRuntime
    from app.adapters.usage import JsonlUsageRuntime
    from app.adapters.workspace import LocalWorkspaceRuntime
    from app.agent.history import ContextManager
    from app.agent.types import AgentRuntime
    from app.application.runtime import ApplicationRuntime
    from app.application.services import SandboxLimits
    from app.audit.logger import AuditLogger
    from app.config.settings import Settings
    from app.execution import ExecutionService
    from app.models.router import ModelRouter
    from app.policy.engine import PolicyEngine
    from app.project.trust import TrustStore
    from app.sessions.store import SessionStore

    root = Path(tmp_path)
    resolved_settings = settings or Settings()
    clock = SystemClock()
    resolved_trace = trace or AuditLogger(path=root / "audit.jsonl")
    resolved_sessions = sessions if sessions is not None else SessionStore(path=root / "sessions.sqlite")
    resolved_model = model or ModelRouter(primary=FakeProvider([]), settings=resolved_settings)
    resolved_execution = execution or ExecutionService(audit=resolved_trace)
    resolved_trust = trust or TrustStore(root / "trust.json")
    resolved_workspace = workspace or LocalWorkspaceRuntime()
    resolved_tools = tools or DefaultToolRuntime()

    resolved_agent = agent or AgentRuntime(
        model_runtime=resolved_model,
        trace=resolved_trace,
        policy=PolicyEngine(),
        execution=resolved_execution,
        trust=resolved_trust,
        tools=resolved_tools,
        workspace=resolved_workspace,
        clock=clock,
        approvals=approvals or SessionApprovalBroker(),
    )
    if resolved_agent.context_manager is None:
        resolved_agent.context_manager = ContextManager(resolved_agent)

    return ApplicationRuntime(
        settings=resolved_settings,
        sessions=resolved_sessions,
        model=resolved_model,
        trace=resolved_trace,
        execution=resolved_execution,
        trust=resolved_trust,
        workspace=resolved_workspace,
        usage=JsonlUsageRuntime(resolved_trace.path),
        clock=clock,
        agent=resolved_agent,
        sandbox_limits=SandboxLimits(cpus="2", memory="2g", pids_limit=256),
    )
