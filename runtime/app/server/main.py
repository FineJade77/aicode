from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.loop_v2 import run_turn_safely
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger, stable_hash
from app.config.settings import settings
from app.events.sse import encode_sse
from app.models.router import ModelRouter
from app.policy.engine import PolicyEngine
from app.project.config import load_project_config
from app.sessions.store import QueuedAgentRun, Session, store
from app.tools.review import review_rules_data
from app.tools.router import ToolRouter
from app.usage.store import summarize_usage

app = FastAPI(title=settings.app_name, version=settings.version)
model_router = ModelRouter.from_settings(settings)
tools = ToolRouter()
audit = AuditLogger.from_env()
agent_runtime = AgentRuntime(model_router=model_router, tools=tools, audit=audit, policy=PolicyEngine())


class CreateSessionRequest(BaseModel):
    workspace: str
    language: str = "zh-CN"


class CreateSessionResponse(BaseModel):
    session_id: str


class MessageRequest(BaseModel):
    message: str
    mode: str = "default"
    workspace: str
    language: str = "zh-CN"


class ApprovalRequest(BaseModel):
    approval_id: str
    accept_all: bool = False


@app.get("/v1/daemon/status")
async def daemon_status() -> dict[str, Any]:
    return {
        "status": "ok",
        "name": settings.app_name,
        "version": settings.version,
        "pid": os.getpid(),
    }


@app.post("/v1/sessions", response_model=CreateSessionResponse)
async def create_session(request: CreateSessionRequest) -> CreateSessionResponse:
    session = store.create(workspace=request.workspace, language=request.language)
    audit.record(
        "session.created",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"language": session.language},
    )
    await session.events.put(
        {
            "type": "session.created",
            "session_id": session.session_id,
            "workspace": session.workspace,
        }
    )
    return CreateSessionResponse(session_id=session.session_id)


@app.get("/v1/sessions")
async def list_sessions(last: bool = False) -> Any:
    if last:
        session = store.last()
        if session is None:
            return None
        return session.to_dict()
    return store.list()


@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    return session.to_dict()


@app.post("/v1/sessions/{session_id}/messages")
async def send_message(session_id: str, request: MessageRequest) -> dict[str, str]:
    session = require_session(session_id)
    effective_request = bind_message_request_to_session(session, request)
    is_configured = getattr(agent_runtime.model_router.primary, "is_configured", None)
    if callable(is_configured) and not is_configured():
        raise HTTPException(status_code=400, detail="模型 provider 未配置，请设置 API key（如 OPENAI_API_KEY 或 ANTHROPIC_API_KEY）后重试")
    was_running = session.agent_runner_active() or session.agent_queue.qsize() > 0
    session.events.set_default_after(session.events.last_event_id())
    queued = session.enqueue_agent_run(effective_request)
    session.events.set_default_run_id(queued.run_id)
    store.append_message(session, effective_request.model_dump())
    queue_position = session.agent_queue.qsize()
    audit.record(
        "message.received",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "mode": effective_request.mode,
            "language": effective_request.language,
            "message_hash": stable_hash(effective_request.message),
            "message_preview": effective_request.message[:200],
            "run_id": queued.run_id,
            "queued": was_running,
            "queue_position": queue_position,
        },
    )
    await emit_run_queued(session, queued, was_running, queue_position)
    ensure_session_runner(session)
    return {"status": "queued" if was_running else "accepted", "run_id": queued.run_id}


@app.get("/v1/sessions/{session_id}/events")
async def stream_events(session_id: str, request: Request, after: int | None = None, run_id: str | None = None) -> StreamingResponse:
    session = require_session(session_id)
    cursor = event_cursor(after, request.headers.get("last-event-id"))
    if cursor is None:
        cursor = session.events.default_after()
    target_run_id = run_id or session.events.default_run_id()

    async def iterator():
        async for event in session.events.subscribe(after=cursor):
            if target_run_id and event.get("run_id") != target_run_id:
                continue
            yield encode_sse(event)
            if event.get("type") == "final":
                break

    return StreamingResponse(iterator(), media_type="text/event-stream")


@app.post("/v1/sessions/{session_id}/approve")
async def approve(session_id: str, request: ApprovalRequest) -> dict[str, str]:
    session = require_session(session_id)
    if request.accept_all:
        session.auto_accept_edits = True
        audit.record(
            "approval.accept_all_enabled",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"approval_id": request.approval_id},
        )
    if not session.resolve_approval(request.approval_id, accepted=True):
        raise HTTPException(status_code=404, detail="approval not found or already resolved")
    audit.record(
        "approval.resolved",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"approval_id": request.approval_id, "accepted": True},
    )
    return {"status": "accepted", "approval_id": request.approval_id}


@app.post("/v1/sessions/{session_id}/reject")
async def reject(session_id: str, request: ApprovalRequest) -> dict[str, str]:
    session = require_session(session_id)
    if not session.resolve_approval(request.approval_id, accepted=False):
        raise HTTPException(status_code=404, detail="approval not found or already resolved")
    audit.record(
        "approval.resolved",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"approval_id": request.approval_id, "accepted": False},
    )
    return {"status": "rejected", "approval_id": request.approval_id}


@app.get("/v1/usage")
async def usage(today: bool = False, session_id: str | None = None) -> dict[str, Any]:
    day = datetime_utc_today() if today else None
    return summarize_usage(audit.path, session_id=session_id, day=day)


@app.get("/v1/usage/sessions/{session_id}")
async def usage_for_session(session_id: str) -> dict[str, Any]:
    return summarize_usage(audit.path, session_id=session_id)


@app.get("/v1/models/routes")
async def model_routes() -> dict[str, Any]:
    return model_router.route_status()


@app.get("/v1/review/rules")
async def review_rules(workspace: str | None = None) -> dict[str, Any]:
    project_config = load_project_config(Path(workspace)) if workspace else None
    if project_config is None:
        return review_rules_data()
    return review_rules_data(
        disabled_rules=project_config.review.disabled_rules,
        large_diff_threshold=project_config.review.large_diff_threshold,
        max_findings=project_config.review.max_findings,
    )


def require_session(session_id: str) -> Session:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def bind_message_request_to_session(session: Session, request: MessageRequest) -> MessageRequest:
    if not same_workspace(session.workspace, request.workspace):
        raise HTTPException(status_code=400, detail="message workspace does not match session workspace")
    return request.model_copy(update={"workspace": session.workspace, "language": session.language})


def same_workspace(left: str, right: str) -> bool:
    if left == right:
        return True
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return False


def event_cursor(after: int | None, last_event_id: str | None) -> int | None:
    if after is not None:
        return after
    if not last_event_id:
        return None
    try:
        return int(last_event_id)
    except ValueError:
        return None


async def run_agent(session: Session, request: MessageRequest) -> None:
    await run_turn_safely(session, request, agent_runtime)


def ensure_session_runner(session: Session) -> None:
    if session.agent_runner_active():
        return
    session.agent_runner_task = asyncio.create_task(process_session_runs(session))


async def process_session_runs(session: Session) -> None:
    while True:
        queued = session.next_agent_run()
        if queued is None:
            return
        await process_session_run(session, queued)


async def process_session_run(session: Session, queued: QueuedAgentRun) -> None:
    session.events.set_current_run_id(queued.run_id)
    try:
        await emit_run_started(session, queued)
        await run_agent(session, queued.request)
    finally:
        session.events.set_current_run_id(None)
        session.finish_agent_run()


async def emit_run_queued(session: Session, queued: QueuedAgentRun, was_running: bool, queue_position: int) -> None:
    message = "任务已排队，等待当前会话中的上一条任务完成。"
    status = "queued"
    if not was_running:
        message = "任务已接收，准备开始执行。"
        status = "accepted"
    await session.events.put(
        {
            "type": "run.queued",
            "run_id": queued.run_id,
            "status": status,
            "queue_position": queue_position,
            "message": message,
        }
    )


async def emit_run_started(session: Session, queued: QueuedAgentRun) -> None:
    await session.events.put(
        {
            "type": "run.started",
            "message": "开始执行当前任务。",
        }
    )


def datetime_utc_today():
    return datetime.now(timezone.utc).date()
