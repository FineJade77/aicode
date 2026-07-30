from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.adapters.composition import build_application_runtime
from app.adapters.usage import JsonlUsageRuntime
from app.agent.loop import AgentLoop
from app.application.contracts import TurnRequest
from app.application.errors import ApplicationError
from app.application.services import (
    ApprovalService,
    ContextService,
    ExecutionApplicationService,
    ModelService,
    ProjectTrustService,
    RunCoordinator,
    SessionService,
    TraceService,
)
from app.config.settings import settings
from app.contracts.api import contract_descriptor
from app.core.session import AgentSession
from app.events.sse import encode_sse
from app.server.auth import auth_middleware

# Idle interval between SSE keep-alive frames. Also the cadence at which a
# run-scoped stream re-checks whether its run can still emit.
SSE_IDLE_TIMEOUT_SECONDS = float(os.getenv("AICODE_SSE_IDLE_TIMEOUT_SECONDS", "15") or 15)

application_runtime = build_application_runtime(settings)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await application_runtime.aclose()


app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
app.middleware("http")(auth_middleware)


class CreateSessionRequest(BaseModel):
    workspace: str


class CreateSessionResponse(BaseModel):
    session_id: str


class SendMessageResponse(BaseModel):
    status: Literal["accepted", "queued"]
    run_id: str


class CancelRunResponse(BaseModel):
    status: Literal["cancelled", "idle"]
    run_id: str | None
    queued: int


class SteerRequest(BaseModel):
    message: str


class SteerResponse(BaseModel):
    status: Literal["queued"]
    run_id: str
    pending: int


class CompactResponse(BaseModel):
    status: Literal["compacted", "unchanged"]
    compaction: dict[str, Any] | None


class PruneSessionsRequest(BaseModel):
    """Omitted bounds fall back to the configured retention policy."""

    max_sessions: int | None = Field(default=None, ge=0)
    max_age_days: int | None = Field(default=None, ge=0)


class PruneSessionsResponse(BaseModel):
    status: Literal["ok", "disabled"]
    deleted_sessions: int
    deleted_messages: int
    retained_live: int


class MessageRequest(BaseModel):
    message: str
    mode: str = "default"
    workspace: str
    model: str | None = None

    def to_contract(self) -> TurnRequest:
        return TurnRequest(
            message=self.message,
            mode=self.mode,
            workspace=self.workspace,
            model=self.model,
        )


class ApprovalRequest(BaseModel):
    approval_id: str
    accept_all: bool = False


class SandboxExecutionRequest(BaseModel):
    execution_id: str
    backend: Literal["docker"] = "docker"
    action: Literal["test", "build", "lint"]
    workspace: str
    timeout_seconds: float = Field(default=1800.0, gt=0, le=3600)


class CancelExecutionResponse(BaseModel):
    status: Literal["cancelled", "idle"]
    execution_id: str


class ExecutionResponse(BaseModel):
    execution_id: str
    backend: str
    action: str
    status: Literal["succeeded", "failed", "timed_out", "cancelled"]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    cancelled: bool


class TrustRequest(BaseModel):
    workspace: str


class TrustStatusResponse(BaseModel):
    workspace: str
    level: Literal["trusted", "untrusted"]
    git_remote: str
    recorded_remote: str
    reason: str
    removed: bool | None = None


class TrustListResponse(BaseModel):
    projects: list[TrustStatusResponse]


def session_service() -> SessionService:
    return SessionService(
        application_runtime.sessions,
        application_runtime.trace,
        application_runtime.workspace,
    )


def run_coordinator() -> RunCoordinator:
    model = application_runtime.agent.model_runtime or application_runtime.model
    return RunCoordinator(
        model,
        application_runtime.trace,
        AgentLoop(application_runtime.agent),
    )


def approval_service() -> ApprovalService:
    return ApprovalService(application_runtime.trace)


def context_service() -> ContextService:
    return ContextService(application_runtime.agent, application_runtime.trace)


def trace_service() -> TraceService:
    return TraceService(
        application_runtime.trace,
        JsonlUsageRuntime(application_runtime.trace.path),
        application_runtime.clock,
    )


def project_trust_service() -> ProjectTrustService:
    return ProjectTrustService(application_runtime.trust, application_runtime.trace)


def execution_application_service() -> ExecutionApplicationService:
    return ExecutionApplicationService(
        application_runtime.execution,
        application_runtime.workspace,
        application_runtime.sandbox_limits,
    )


def model_service() -> ModelService:
    return ModelService(application_runtime.model)


def raise_http_error(exc: ApplicationError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/v1/daemon/status")
async def daemon_status() -> dict[str, Any]:
    return application_runtime.status(pid=os.getpid())


@app.get("/v1/meta/contract")
async def api_contract() -> dict[str, Any]:
    return contract_descriptor(settings.version)


@app.post("/v1/executions", response_model=ExecutionResponse)
async def execute_sandbox(request: SandboxExecutionRequest) -> dict[str, Any]:
    try:
        return await execution_application_service().execute(request)
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/executions/{execution_id}/cancel", response_model=CancelExecutionResponse)
async def cancel_execution(execution_id: str) -> dict[str, str]:
    return await execution_application_service().cancel(execution_id)


@app.post("/v1/daemon/prepare-stop")
async def prepare_daemon_stop() -> dict[str, Any]:
    return await application_runtime.prepare_stop()


@app.get("/v1/trust", response_model=TrustStatusResponse | TrustListResponse)
async def get_trust(workspace: str | None = None) -> Any:
    try:
        return project_trust_service().get(workspace)
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/trust", response_model=TrustStatusResponse)
async def trust_project(request: TrustRequest) -> dict[str, Any]:
    try:
        return project_trust_service().set_trusted(request.workspace)
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/trust/remove", response_model=TrustStatusResponse)
async def remove_project_trust(request: TrustRequest) -> dict[str, Any]:
    try:
        return project_trust_service().remove(request.workspace)
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/sessions", response_model=CreateSessionResponse)
async def create_session(request: CreateSessionRequest) -> CreateSessionResponse:
    session = await session_service().create(request.workspace)
    return CreateSessionResponse(session_id=session.session_id)


@app.get("/v1/sessions")
async def list_sessions(last: bool = False, limit: int | None = None, offset: int = 0) -> Any:
    result = session_service().list(last=last, limit=limit, offset=offset)
    if result is None:
        return None
    if isinstance(result, list):
        return [session.to_dict() for session in result]
    return result.to_dict()


@app.post("/v1/sessions/prune", response_model=PruneSessionsResponse)
async def prune_sessions(request: PruneSessionsRequest) -> dict[str, Any]:
    return session_service().prune(
        max_sessions=request.max_sessions if request.max_sessions is not None else settings.session_retention.max_sessions,
        max_age_days=request.max_age_days if request.max_age_days is not None else settings.session_retention.max_age_days,
    )


@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    try:
        return session_service().get(session_id).to_dict()
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/sessions/{session_id}/messages", response_model=SendMessageResponse)
async def send_message(session_id: str, request: MessageRequest) -> dict[str, str]:
    session = require_session(session_id)
    effective_request = bind_message_request_to_session(session, request)
    try:
        return (await run_coordinator().submit(session, effective_request)).to_dict()
    except ApplicationError as exc:
        raise_http_error(exc)


@app.get("/v1/sessions/{session_id}/events")
async def stream_events(session_id: str, request: Request, after: int | None = None, run_id: str | None = None) -> StreamingResponse:
    session = require_session(session_id)
    cursor = event_cursor(after, request.headers.get("last-event-id"))
    if cursor is None:
        cursor = session.events.default_after()
    target_run_id = run_id or session.events.default_run_id()

    async def iterator():
        # A run-scoped stream must always reach a terminal event. The client
        # treats a stream that ends without `final` as a retryable disconnect and
        # reconnects, so a run that can no longer produce events would otherwise
        # put the CLI in an endless reconnect loop.
        if target_run_id:
            terminal = unreachable_run_terminal(session, target_run_id, cursor)
            if terminal is not None:
                yield encode_sse(terminal)
                return

        async for event in session.events.subscribe(after=cursor, idle_timeout=SSE_IDLE_TIMEOUT_SECONDS):
            if event is None:
                if target_run_id:
                    terminal = unreachable_run_terminal(session, target_run_id, cursor)
                    if terminal is not None:
                        yield encode_sse(terminal)
                        return
                # Proves the connection is alive to both the client and any
                # intermediary that would otherwise drop an idle stream.
                yield ": keep-alive\n\n"
                continue
            if target_run_id and event.get("run_id") != target_run_id:
                continue
            yield encode_sse(event)
            if event.get("type") == "final":
                break

    return StreamingResponse(iterator(), media_type="text/event-stream")


@app.post("/v1/sessions/{session_id}/approve")
async def approve(session_id: str, request: ApprovalRequest) -> dict[str, str]:
    session = require_session(session_id)
    try:
        return approval_service().resolve(
            session,
            request.approval_id,
            accepted=True,
            accept_all=request.accept_all,
        )
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/sessions/{session_id}/reject")
async def reject(session_id: str, request: ApprovalRequest) -> dict[str, str]:
    session = require_session(session_id)
    try:
        return approval_service().resolve(session, request.approval_id, accepted=False)
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/sessions/{session_id}/cancel", response_model=CancelRunResponse)
async def cancel_run(session_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    return (await run_coordinator().cancel(session)).to_dict()


@app.post("/v1/sessions/{session_id}/steer", response_model=SteerResponse)
async def steer_run(session_id: str, request: SteerRequest) -> dict[str, Any]:
    session = require_session(session_id)
    try:
        return (await run_coordinator().steer(session, request.message)).to_dict()
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/sessions/{session_id}/compact", response_model=CompactResponse)
async def compact_session(session_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    try:
        return (await context_service().compact(session)).to_dict()
    except ApplicationError as exc:
        raise_http_error(exc)


@app.get("/v1/usage")
async def usage(today: bool = False, session_id: str | None = None) -> dict[str, Any]:
    return await trace_service().summarize(today=today, session_id=session_id)


@app.get("/v1/usage/sessions/{session_id}")
async def usage_for_session(session_id: str) -> dict[str, Any]:
    return await trace_service().summarize(session_id=session_id)


@app.get("/v1/models/routes")
async def model_routes() -> dict[str, Any]:
    return model_service().routes()


@app.get("/v1/models/probe")
async def model_probe(tools: bool = True, model: str | None = None) -> dict[str, Any]:
    return await model_service().probe(model=model, tools=tools)


@app.get("/v1/review/rules")
async def review_rules(workspace: str | None = None) -> dict[str, Any]:
    return application_runtime.workspace.review_rules(workspace)


def require_session(session_id: str) -> AgentSession:
    try:
        return session_service().require(session_id)
    except ApplicationError as exc:
        raise_http_error(exc)


def bind_message_request_to_session(session: AgentSession, request: MessageRequest) -> TurnRequest:
    try:
        return session_service().bind_turn(session, request.to_contract())
    except ApplicationError as exc:
        raise_http_error(exc)


def same_workspace(left: str, right: str) -> bool:
    return application_runtime.workspace.same_workspace(left, right)


def unreachable_run_terminal(
    session: AgentSession,
    run_id: str,
    cursor: int | None,
) -> dict[str, Any] | None:
    """A terminal event to close a stream whose run can no longer emit, else None.

    Two cases produce a stream that would otherwise wait forever:

    * The run already finished at or before the requested cursor — a client that
      reconnected past its own `final`. Its recorded terminal event is replayed.
    * The run has no retained events and is neither running nor queued. Event
      persistence is best-effort, so a `final` can be missing after the session
      was evicted and rebuilt, and a stale run id looks identical. A synthesized
      terminal event is emitted so the client stops instead of reconnecting; it is
      not written to the session, because nothing new actually happened.
    """
    terminal = session.events.final_event_for_run(run_id)
    if terminal is not None:
        if cursor is not None and int(terminal.get("event_id") or 0) <= cursor:
            return terminal
        return None
    if session.events.has_events_for_run(run_id):
        return None
    if session.current_run_id == run_id or run_id in session.queued_run_ids():
        return None
    return {
        "type": "final",
        "run_id": run_id,
        "status": "unavailable",
        "summary": (
            f"No events are available for run {run_id}. It is not running, and its history is no "
            "longer retained; start a new run or stream without a run_id filter."
        ),
    }


def event_cursor(after: int | None, last_event_id: str | None) -> int | None:
    if after is not None:
        return after
    if not last_event_id:
        return None
    try:
        return int(last_event_id)
    except ValueError:
        return None
