from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config.settings import settings
from app.events.sse import encode_sse
from app.models.provider import ModelRequest, StubProvider
from app.sessions.store import Session, store

app = FastAPI(title=settings.app_name, version=settings.version)
provider = StubProvider()


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
    session.messages.append(request.model_dump())
    asyncio.create_task(run_agent(session, request))
    return {"status": "accepted"}


@app.get("/v1/sessions/{session_id}/events")
async def stream_events(session_id: str) -> StreamingResponse:
    session = require_session(session_id)

    async def iterator():
        while True:
            event = await session.events.get()
            yield encode_sse(event)
            if event.get("type") == "final":
                break

    return StreamingResponse(iterator(), media_type="text/event-stream")


@app.post("/v1/sessions/{session_id}/approve")
async def approve(session_id: str, request: ApprovalRequest) -> dict[str, str]:
    require_session(session_id)
    return {"status": "accepted", "approval_id": request.approval_id}


@app.post("/v1/sessions/{session_id}/reject")
async def reject(session_id: str, request: ApprovalRequest) -> dict[str, str]:
    require_session(session_id)
    return {"status": "rejected", "approval_id": request.approval_id}


@app.get("/v1/usage")
async def usage() -> dict[str, Any]:
    return {
        "storage": "local",
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "estimated_cost": 0.0,
        "note": "usage persistence will be implemented in Phase 2",
    }


def require_session(session_id: str) -> Session:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


async def run_agent(session: Session, request: MessageRequest) -> None:
    plan_items = [
        {"id": "scan", "text": localized(request.language, "扫描当前工作区", "Scan current workspace"), "status": "pending"},
        {"id": "context", "text": localized(request.language, "收集最小上下文", "Gather minimal context"), "status": "pending"},
        {"id": "summary", "text": localized(request.language, "输出阶段性结果", "Return phase summary"), "status": "pending"},
    ]
    await session.events.put({"type": "plan.created", "items": plan_items})

    await asyncio.sleep(0.05)
    await session.events.put({"type": "plan.updated", "item_id": "scan", "status": "in_progress"})
    await session.events.put({"type": "tool.started", "tool": "workspace.inspect", "args": {"workspace": request.workspace}})

    files = list_workspace_files(Path(request.workspace))
    await session.events.put(
        {
            "type": "tool.output",
            "tool": "workspace.inspect",
            "text": format_workspace_output(request.language, request.workspace, files),
        }
    )
    await session.events.put({"type": "plan.updated", "item_id": "scan", "status": "completed"})

    await asyncio.sleep(0.05)
    await session.events.put({"type": "plan.updated", "item_id": "context", "status": "completed"})

    response = await provider.complete(
        ModelRequest(
            purpose="summarizer",
            model="stub",
            messages=[{"role": "user", "content": request.message}],
        )
    )
    await session.events.put(
        {
            "type": "usage.recorded",
            "model": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "estimated_cost": response.estimated_cost,
        }
    )
    await session.events.put({"type": "plan.updated", "item_id": "summary", "status": "completed"})
    await session.events.put(
        {
            "type": "final",
            "summary": localized(
                request.language,
                "Phase 0 Runtime 已连通。当前骨架已能创建会话、展示实时计划、检查工作区并通过 SSE 返回事件。",
                "Phase 0 runtime is connected. The scaffold can create sessions, show a live plan, inspect the workspace, and stream events over SSE.",
            ),
        }
    )


def list_workspace_files(workspace: Path) -> list[str]:
    if not workspace.exists() or not workspace.is_dir():
        return []

    ignored = {".git", ".venv", "node_modules", "__pycache__"}
    files: list[str] = []
    for child in sorted(workspace.iterdir(), key=lambda item: item.name):
        if child.name in ignored:
            continue
        suffix = "/" if child.is_dir() else ""
        files.append(child.name + suffix)
        if len(files) >= 12:
            break
    return files


def format_workspace_output(language: str, workspace: str, files: list[str]) -> str:
    if language.startswith("en"):
        if not files:
            return f"Workspace: {workspace}\nNo top-level files found."
        return "Workspace: " + workspace + "\nTop-level files:\n" + "\n".join(f"- {item}" for item in files)
    if not files:
        return f"工作区: {workspace}\n未发现顶层文件。"
    return "工作区: " + workspace + "\n顶层文件:\n" + "\n".join(f"- {item}" for item in files)


def localized(language: str, zh: str, en: str) -> str:
    return en if language.startswith("en") else zh
