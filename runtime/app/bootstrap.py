"""Application composition root."""

from __future__ import annotations

import os

from app.agent.history import ContextManager
from app.agent.policy import PolicyEngine
from app.agent.types import AgentRuntime
from app.application.runtime import ApplicationRuntime
from app.application.services import SandboxLimits
from app.audit.logger import AuditLogger
from app.audit.otel import OtelSpanEmitter, otel_enabled, otel_endpoint, otel_service_name
from app.audit.spans import SpanTraceSink
from app.config import Settings
from app.execution import ExecutionService
from app.models.router import ModelRouter
from app.project.trust import TrustStore
from app.sessions.approvals import SessionApprovalBroker
from app.sessions.store import SessionStore
from app.system import SystemClock, UuidGenerator
from app.tools.runtime import DefaultToolRuntime
from app.tools.workspace import LocalWorkspaceRuntime
from app.usage.store import JsonlUsageRuntime


def build_trace_sink():
    """JSONL audit trail, optionally wrapped so it also emits spans.

    The JSONL sink stays the source of truth and tracing is additive: losing a
    tracing backend must never cost an audit record.
    """
    trace = AuditLogger.from_env()
    if not otel_enabled():
        return trace
    return SpanTraceSink(
        trace,
        OtelSpanEmitter(endpoint=otel_endpoint(), service_name=otel_service_name()),
    )


def build_application_runtime(settings: Settings) -> ApplicationRuntime:
    """Build all concrete adapters in one place."""

    clock = SystemClock()
    ids = UuidGenerator()
    trace = build_trace_sink()
    sessions = SessionStore(clock=clock, ids=ids)
    model = ModelRouter.from_settings(settings)
    execution = ExecutionService(audit=trace)
    trust = TrustStore.from_env()
    workspace = LocalWorkspaceRuntime()
    tools = DefaultToolRuntime()
    agent = AgentRuntime(
        model_runtime=model,
        trace=trace,
        policy=PolicyEngine(),
        execution=execution,
        trust=trust,
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
