from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.session import AgentSession
from app.application.contracts import TurnRequest, contract_descriptor
from app.application.errors import ApplicationError
from app.application.runtime import ApplicationRuntime
from app.bootstrap import build_application_runtime
from app.config import settings
from app.events import encode_sse
from app.server.auth import auth_middleware

# Idle interval between SSE keep-alive frames. Also the cadence at which a
# run-scoped stream re-checks whether its run can still emit.
SSE_IDLE_TIMEOUT_SECONDS = float(os.getenv("AICODE_SSE_IDLE_TIMEOUT_SECONDS", "15") or 15)


@asynccontextmanager
async def lifespan(instance: FastAPI):
    """Own the runtime's whole lifetime.

    Building at import time opened SQLite and provider clients as a side effect of
    importing this module, made the module order-dependent, and made two
    differently configured runtimes impossible in one process.
    """
    instance.state.runtime = build_application_runtime(settings)
    try:
        yield
    finally:
        await instance.state.runtime.aclose()
        instance.state.runtime = None


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


def get_runtime(request: Request) -> ApplicationRuntime:
    """Resolve the per-application runtime built by the ASGI lifespan.

    Handlers take this as a dependency rather than reading a module global, so a
    single process can host more than one differently configured Runtime and
    tests can inject one instead of monkeypatching shared state.
    """
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - only reachable if lifespan was skipped
        raise RuntimeError("application runtime is not initialised; the ASGI lifespan did not run")
    return runtime


RuntimeDep = Annotated[ApplicationRuntime, Depends(get_runtime)]


def raise_http_error(exc: ApplicationError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/v1/daemon/status")
async def daemon_status(runtime: RuntimeDep) -> dict[str, Any]:
    return runtime.status(pid=os.getpid())


@app.get("/v1/meta/contract")
async def api_contract() -> dict[str, Any]:
    return contract_descriptor(settings.version)


@app.post("/v1/executions", response_model=ExecutionResponse)
async def execute_sandbox(request: SandboxExecutionRequest, runtime: RuntimeDep) -> dict[str, Any]:
    try:
        return await runtime.executions.execute(request)
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/executions/{execution_id}/cancel", response_model=CancelExecutionResponse)
async def cancel_execution(execution_id: str, runtime: RuntimeDep) -> dict[str, str]:
    return await runtime.executions.cancel(execution_id)


@app.post("/v1/daemon/prepare-stop")
async def prepare_daemon_stop(runtime: RuntimeDep) -> dict[str, Any]:
    return await runtime.prepare_stop()


@app.get("/v1/trust", response_model=TrustStatusResponse | TrustListResponse)
async def get_trust(runtime: RuntimeDep, workspace: str | None = None) -> Any:
    try:
        return runtime.projects.get(workspace)
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/trust", response_model=TrustStatusResponse)
async def trust_project(request: TrustRequest, runtime: RuntimeDep) -> dict[str, Any]:
    try:
        return runtime.projects.set_trusted(request.workspace)
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/trust/remove", response_model=TrustStatusResponse)
async def remove_project_trust(request: TrustRequest, runtime: RuntimeDep) -> dict[str, Any]:
    try:
        return runtime.projects.remove(request.workspace)
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/sessions", response_model=CreateSessionResponse)
async def create_session(request: CreateSessionRequest, runtime: RuntimeDep) -> CreateSessionResponse:
    session = await runtime.session_service.create(request.workspace)
    return CreateSessionResponse(session_id=session.session_id)


@app.get("/v1/sessions")
async def list_sessions(runtime: RuntimeDep, last: bool = False, limit: int | None = None, offset: int = 0) -> Any:
    result = runtime.session_service.list(last=last, limit=limit, offset=offset)
    if result is None:
        return None
    if isinstance(result, list):
        return [session.to_dict() for session in result]
    return result.to_dict()


@app.post("/v1/sessions/prune", response_model=PruneSessionsResponse)
async def prune_sessions(request: PruneSessionsRequest, runtime: RuntimeDep) -> dict[str, Any]:
    return runtime.session_service.prune(
        max_sessions=request.max_sessions if request.max_sessions is not None else settings.session_retention.max_sessions,
        max_age_days=request.max_age_days if request.max_age_days is not None else settings.session_retention.max_age_days,
    )


@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str, runtime: RuntimeDep) -> dict[str, Any]:
    try:
        return runtime.session_service.get(session_id).to_dict()
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/sessions/{session_id}/messages", response_model=SendMessageResponse)
async def send_message(session_id: str, request: MessageRequest, runtime: RuntimeDep) -> dict[str, str]:
    session = require_session(runtime, session_id)
    effective_request = bind_message_request_to_session(runtime, session, request)
    try:
        return (await runtime.runs.submit(session, effective_request)).to_dict()
    except ApplicationError as exc:
        raise_http_error(exc)


@app.get("/v1/sessions/{session_id}/events")
async def stream_events(session_id: str, request: Request, runtime: RuntimeDep, after: int | None = None, run_id: str | None = None) -> StreamingResponse:
    session = require_session(runtime, session_id)
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
async def approve(session_id: str, request: ApprovalRequest, runtime: RuntimeDep) -> dict[str, str]:
    session = require_session(runtime, session_id)
    try:
        return runtime.approvals.resolve(
            session,
            request.approval_id,
            accepted=True,
            accept_all=request.accept_all,
        )
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/sessions/{session_id}/reject")
async def reject(session_id: str, request: ApprovalRequest, runtime: RuntimeDep) -> dict[str, str]:
    session = require_session(runtime, session_id)
    try:
        return runtime.approvals.resolve(session, request.approval_id, accepted=False)
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/sessions/{session_id}/cancel", response_model=CancelRunResponse)
async def cancel_run(session_id: str, runtime: RuntimeDep) -> dict[str, Any]:
    session = require_session(runtime, session_id)
    return (await runtime.runs.cancel(session)).to_dict()


@app.post("/v1/sessions/{session_id}/steer", response_model=SteerResponse)
async def steer_run(session_id: str, request: SteerRequest, runtime: RuntimeDep) -> dict[str, Any]:
    session = require_session(runtime, session_id)
    try:
        return (await runtime.runs.steer(session, request.message)).to_dict()
    except ApplicationError as exc:
        raise_http_error(exc)


@app.post("/v1/sessions/{session_id}/compact", response_model=CompactResponse)
async def compact_session(session_id: str, runtime: RuntimeDep) -> dict[str, Any]:
    session = require_session(runtime, session_id)
    try:
        return (await runtime.contexts.compact(session)).to_dict()
    except ApplicationError as exc:
        raise_http_error(exc)


@app.get("/v1/usage")
async def usage(runtime: RuntimeDep, today: bool = False, session_id: str | None = None) -> dict[str, Any]:
    return await runtime.traces.summarize(today=today, session_id=session_id)


@app.get("/v1/usage/sessions/{session_id}")
async def usage_for_session(session_id: str, runtime: RuntimeDep) -> dict[str, Any]:
    return await runtime.traces.summarize(session_id=session_id)


@app.get("/v1/models/routes")
async def model_routes(runtime: RuntimeDep) -> dict[str, Any]:
    return runtime.models.routes()


@app.get("/v1/models/probe")
async def model_probe(runtime: RuntimeDep, tools: bool = True, model: str | None = None) -> dict[str, Any]:
    return await runtime.models.probe(model=model, tools=tools)


@app.get("/v1/review/rules")
async def review_rules(runtime: RuntimeDep, workspace: str | None = None) -> dict[str, Any]:
    return runtime.workspace.review_rules(workspace)


def require_session(runtime: ApplicationRuntime, session_id: str) -> AgentSession:
    try:
        return runtime.session_service.require(session_id)
    except ApplicationError as exc:
        raise_http_error(exc)


def bind_message_request_to_session(
    runtime: ApplicationRuntime,
    session: AgentSession,
    request: MessageRequest,
) -> TurnRequest:
    try:
        return runtime.session_service.bind_turn(session, request.to_contract())
    except ApplicationError as exc:
        raise_http_error(exc)


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
