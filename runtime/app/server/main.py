from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.commands import detect_append_request, detect_create_request, detect_replace_request, detect_shell_request
from app.agent.loop import run_agent_safely as agent_run_agent_safely
from app.agent.patch_flow import run_post_patch_verification as agent_run_post_patch_verification
from app.agent.summary import build_model_messages, final_summary_text, model_purpose_for_mode
from app.agent.tool_flow import execute_tool as agent_execute_tool
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger, stable_hash
from app.config.settings import settings
from app.events.sse import encode_sse
from app.models.router import ModelRouter
from app.project.config import load_project_config
from app.sessions.store import Session, store
from app.tools.review import review_rules_data
from app.tools.router import ToolRouter
from app.usage.store import summarize_usage

app = FastAPI(title=settings.app_name, version=settings.version)
model_router = ModelRouter.from_settings(settings)
tools = ToolRouter()
audit = AuditLogger.from_env()
agent_runtime = AgentRuntime(model_router=model_router, tools=tools, audit=audit)


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
    session.events.set_default_after(session.events.last_event_id())
    store.append_message(session, effective_request.model_dump())
    audit.record(
        "message.received",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "mode": effective_request.mode,
            "language": effective_request.language,
            "message_hash": stable_hash(effective_request.message),
            "message_preview": effective_request.message[:200],
        },
    )
    asyncio.create_task(run_agent(session, effective_request))
    return {"status": "accepted"}


@app.get("/v1/sessions/{session_id}/events")
async def stream_events(session_id: str, request: Request, after: int | None = None) -> StreamingResponse:
    session = require_session(session_id)
    cursor = event_cursor(after, request.headers.get("last-event-id"))
    if cursor is None:
        cursor = session.events.default_after()

    async def iterator():
        async for event in session.events.subscribe(after=cursor):
            yield encode_sse(event)
            if event.get("type") == "final":
                break

    return StreamingResponse(iterator(), media_type="text/event-stream")


@app.post("/v1/sessions/{session_id}/approve")
async def approve(session_id: str, request: ApprovalRequest) -> dict[str, str]:
    session = require_session(session_id)
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
    await agent_run_agent_safely(session, request, agent_runtime)


async def execute_tool(session: Session, request: MessageRequest, name: str, args: dict[str, Any]):
    return await agent_execute_tool(session, request, name, args, agent_runtime)


async def run_post_patch_verification(session: Session, request: MessageRequest) -> dict[str, Any]:
    return await agent_run_post_patch_verification(session, request, agent_runtime)


def datetime_utc_today():
    return datetime.now(timezone.utc).date()
