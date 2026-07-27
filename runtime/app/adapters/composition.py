from __future__ import annotations

import os

from app.adapters.approvals import SessionApprovalBroker
from app.adapters.system import SystemClock, UuidGenerator
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


def build_application_runtime(settings: Settings) -> ApplicationRuntime:
    """Build all concrete adapters in one place."""

    clock = SystemClock()
    ids = UuidGenerator()
    trace = AuditLogger.from_env()
    sessions = SessionStore(clock=clock, ids=ids)
    model = ModelRouter.from_settings(settings)
    execution = ExecutionService(audit=trace)
    trust = TrustStore.from_env()
    workspace = LocalWorkspaceRuntime()
    tools = DefaultToolRuntime()
    agent = AgentRuntime(
        model_router=model,
        audit=trace,
        policy=PolicyEngine(),
        execution=execution,
        trust_store=trust,
        tools=tools,
        workspace=workspace,
        clock=clock,
        approvals=SessionApprovalBroker(),
    )
    agent.context_manager = ContextManager(agent)
    return ApplicationRuntime(
        settings=settings,
        sessions=sessions,
        model=model,
        trace=trace,
        execution=execution,
        trust=trust,
        workspace=workspace,
        usage=JsonlUsageRuntime(trace.path),
        clock=clock,
        agent=agent,
        sandbox_limits=SandboxLimits(
            cpus=os.getenv("AICODE_SANDBOX_CPUS", "2"),
            memory=os.getenv("AICODE_SANDBOX_MEMORY", "2g"),
            pids_limit=int(os.getenv("AICODE_SANDBOX_PIDS_LIMIT", "256")),
        ),
    )
