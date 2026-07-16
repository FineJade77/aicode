from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.audit.logger import AuditLogger, stable_hash
from app.config.settings import settings
from app.events.sse import encode_sse
from app.models.provider import ModelRequest
from app.models.router import ModelRouter
from app.sessions.store import Session, store
from app.tools.base import ToolError
from app.tools.patch import apply_content_patch, create_append_patch
from app.tools.router import ToolRouter
from app.usage.store import summarize_usage

app = FastAPI(title=settings.app_name, version=settings.version)
model_router = ModelRouter.from_settings(settings)
tools = ToolRouter()
audit = AuditLogger.from_env()


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
    session.messages.append(request.model_dump())
    audit.record(
        "message.received",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "mode": request.mode,
            "language": request.language,
            "message_hash": stable_hash(request.message),
            "message_preview": request.message[:200],
        },
    )
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


def require_session(session_id: str) -> Session:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


async def run_agent(session: Session, request: MessageRequest) -> None:
    plan_items = [
        {"id": "scan", "text": localized(request.language, "扫描当前工作区", "Scan current workspace"), "status": "pending"},
        {"id": "context", "text": localized(request.language, "调用真实工具收集上下文", "Gather context with real tools"), "status": "pending"},
        {"id": "summary", "text": localized(request.language, "输出阶段性结果", "Return phase summary"), "status": "pending"},
    ]
    await session.events.put({"type": "plan.created", "items": plan_items})

    await asyncio.sleep(0.05)
    await session.events.put({"type": "plan.updated", "item_id": "scan", "status": "in_progress"})
    await execute_tool(session, request, "list_files", {"path": ".", "max_depth": 1, "limit": 40})
    await execute_tool(session, request, "git_status", {})
    await session.events.put({"type": "plan.updated", "item_id": "scan", "status": "completed"})

    await asyncio.sleep(0.05)
    await session.events.put({"type": "plan.updated", "item_id": "context", "status": "in_progress"})
    for tool_name, args in choose_context_tools(request):
        await execute_tool(session, request, tool_name, args)
    append_request = detect_append_request(request.message)
    if append_request is not None:
        await propose_append_patch(session, request, append_request[0], append_request[1])
    await session.events.put({"type": "plan.updated", "item_id": "context", "status": "completed"})

    response = await model_router.complete(
        ModelRequest(
            purpose="summarizer",
            messages=[{"role": "user", "content": request.message}],
        )
    )
    audit.record(
        "usage.recorded",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "model": response.model,
            "provider": response.provider,
            "purpose": "summarizer",
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "estimated_cost": response.estimated_cost,
        },
    )
    await session.events.put(
        {
            "type": "usage.recorded",
            "model": response.model,
            "provider": response.provider,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "estimated_cost": response.estimated_cost,
        }
    )
    await session.events.put({"type": "plan.updated", "item_id": "summary", "status": "completed"})
    audit.record("session.final", session_id=session.session_id, workspace=session.workspace, data={"mode": request.mode})
    await session.events.put(
        {
            "type": "final",
            "summary": localized(
                request.language,
                "Phase 1 工具系统已连通。Runtime 已通过结构化工具读取工作区、检查 git 状态，并支持写入前 inline diff 确认。",
                "Phase 1 tool system is connected. The runtime used structured tools to inspect the workspace, check git status, and supports inline diff approval before writes.",
            ),
        }
    )


async def execute_tool(session: Session, request: MessageRequest, name: str, args: dict[str, Any]) -> None:
    decision = tools.evaluate(name, args, mode=request.mode)
    audit.record(
        "tool.started",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "tool": name,
            "args": args,
            "risk_level": decision.risk_level,
            "requires_approval": decision.requires_approval,
        },
    )
    await session.events.put(
        {
            "type": "tool.started",
            "tool": name,
            "args": args,
            "risk_level": decision.risk_level,
            "requires_approval": decision.requires_approval,
        }
    )

    result = await tools.run(name, args, workspace=request.workspace, mode=request.mode, language=request.language)
    if result.success:
        audit.record(
            "tool.completed",
            session_id=session.session_id,
            workspace=session.workspace,
            data={
                "tool": name,
                "risk_level": result.risk_level,
                "requires_approval": result.requires_approval,
                "output_hash": stable_hash(result.text),
                "data": result.data,
            },
        )
        await session.events.put(
            {
                "type": "tool.output",
                "tool": name,
                "text": result.text,
                "data": result.data,
                "risk_level": result.risk_level,
                "requires_approval": result.requires_approval,
            }
        )
        return

    event_type = "tool.denied" if result.requires_approval or result.risk_level == "high" else "tool.error"
    audit.record(
        event_type,
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "tool": name,
            "error": result.error,
            "risk_level": result.risk_level,
            "requires_approval": result.requires_approval,
            "data": result.data,
        },
    )
    await session.events.put(
        {
            "type": event_type,
            "tool": name,
            "error": result.error,
            "data": result.data,
            "risk_level": result.risk_level,
            "requires_approval": result.requires_approval,
        }
    )


def choose_context_tools(request: MessageRequest) -> list[tuple[str, dict[str, Any]]]:
    message = request.message.strip()
    lowered = message.lower()

    if detect_append_request(message) is not None:
        return []

    if request.mode in {"review", "diff"} or "diff" in lowered or "变更" in message or "审查" in message:
        return [("git_diff", {})]

    if request.mode == "test" or "运行测试" in message or "run tests" in lowered:
        command = detect_test_command(Path(request.workspace))
        if command:
            return [("run_shell", {"command": command, "timeout": 120})]
        return [("search_text", {"query": "test", "limit": 40})]

    target = extract_target(message)
    if target:
        return [("read_file", {"path": target, "max_bytes": 30_000})]

    keyword = extract_keyword(message)
    if keyword:
        return [("search_text", {"query": keyword, "limit": 40})]

    return []


async def propose_append_patch(session: Session, request: MessageRequest, path: str, text: str) -> None:
    if request.mode == "review":
        await session.events.put(
            {
                "type": "tool.denied",
                "tool": "apply_patch",
                "error": localized(request.language, "review 模式禁止写入", "review mode forbids writes"),
                "risk_level": "high",
                "requires_approval": False,
            }
        )
        return

    try:
        proposal = create_append_patch(Path(request.workspace), path, text)
    except (ToolError, UnicodeDecodeError) as exc:
        audit.record(
            "tool.error",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"tool": "generate_patch", "error": str(exc), "risk_level": "medium", "requires_approval": True},
        )
        await session.events.put(
            {
                "type": "tool.error",
                "tool": "generate_patch",
                "error": str(exc),
                "risk_level": "medium",
                "requires_approval": True,
            }
        )
        return

    approval = session.create_approval(
        "patch",
        {
            "operation": "append",
            "path": proposal.path,
            "new_content": proposal.new_content,
        },
    )
    audit.record(
        "approval.requested",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "approval_id": approval.approval_id,
            "kind": "patch",
            "files": [proposal.path],
            "patch_hash": stable_hash(proposal.diff),
        },
    )
    await session.events.put(
        {
            "type": "approval.requested",
            "approval_id": approval.approval_id,
            "kind": "patch",
            "risk_level": "medium",
            "message": localized(
                request.language,
                f"是否允许修改 {proposal.path}？",
                f"Allow changes to {proposal.path}?",
            ),
        }
    )
    await session.events.put(
        {
            "type": "patch.preview",
            "approval_id": approval.approval_id,
            "files": [proposal.path],
            "diff": proposal.diff,
        }
    )

    accepted = await session.wait_for_approval(approval.approval_id)
    if accepted is None:
        audit.record(
            "patch.rejected",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"approval_id": approval.approval_id, "reason": "timeout", "files": [proposal.path]},
        )
        await session.events.put(
            {
                "type": "patch.rejected",
                "approval_id": approval.approval_id,
                "reason": localized(request.language, "等待确认超时", "approval timed out"),
            }
        )
        return
    if not accepted:
        audit.record(
            "patch.rejected",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"approval_id": approval.approval_id, "reason": "user_rejected", "files": [proposal.path]},
        )
        await session.events.put(
            {
                "type": "patch.rejected",
                "approval_id": approval.approval_id,
                "reason": localized(request.language, "用户拒绝修改", "user rejected the patch"),
            }
        )
        return

    try:
        apply_content_patch(Path(request.workspace), proposal.path, proposal.new_content)
    except ToolError as exc:
        await session.events.put(
            {
                "type": "tool.error",
                "tool": "apply_patch",
                "error": str(exc),
                "risk_level": "medium",
                "requires_approval": True,
            }
        )
        return

    audit.record(
        "patch.applied",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"approval_id": approval.approval_id, "files": [proposal.path], "patch_hash": stable_hash(proposal.diff)},
    )
    await session.events.put(
        {
            "type": "patch.applied",
            "approval_id": approval.approval_id,
            "files": [proposal.path],
        }
    )


def detect_append_request(message: str) -> tuple[str, str] | None:
    stripped = message.strip()
    lowered = stripped.lower()
    prefixes = ["append ", "追加 "]
    for prefix in prefixes:
        if lowered.startswith(prefix) or stripped.startswith(prefix):
            body = stripped[len(prefix) :].strip()
            path, text = split_path_and_text(body)
            if path and text:
                return path, text
    return None


def split_path_and_text(body: str) -> tuple[str | None, str | None]:
    if " " not in body:
        return None, None
    path, text = body.split(" ", 1)
    text = text.strip()
    if not path or not text:
        return None, None
    return path.strip("，。,. "), text


def extract_target(message: str) -> str | None:
    parts = message.split()
    for part in reversed(parts):
        if "/" in part or "." in part:
            cleaned = part.strip("，。,. ")
            if cleaned and not cleaned.startswith("http"):
                return cleaned
    return None


def detect_test_command(workspace: Path) -> str | None:
    if (workspace / "go.mod").exists():
        return "go test ./..."

    go_work = workspace / "go.work"
    if go_work.exists():
        modules = parse_go_work_modules(go_work)
        if modules:
            packages = " ".join(f"{module}/..." for module in modules)
            return f"go test {packages}"

    if (workspace / "pyproject.toml").exists() or (workspace / "pytest.ini").exists() or (workspace / "setup.cfg").exists():
        return "python3 -m pytest"

    if (workspace / "package.json").exists():
        if (workspace / "pnpm-lock.yaml").exists():
            return "pnpm test"
        if (workspace / "yarn.lock").exists():
            return "yarn test"
        return "npm test"

    return None


def parse_go_work_modules(go_work: Path) -> list[str]:
    modules: list[str] = []
    in_use_block = False
    for raw in go_work.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line == "use (":
            in_use_block = True
            continue
        if in_use_block and line == ")":
            in_use_block = False
            continue
        if line.startswith("use "):
            modules.append(line.removeprefix("use ").strip())
            continue
        if in_use_block:
            modules.append(line)
    return [module for module in modules if module.startswith("./")]


def datetime_utc_today():
    return datetime.now(timezone.utc).date()


def extract_keyword(message: str) -> str | None:
    for token in ["login", "auth", "test", "pytest", "go test", "错误", "失败", "测试", "登录", "认证"]:
        if token in message.lower() or token in message:
            return token
    return None


def localized(language: str, zh: str, en: str) -> str:
    return en if language.startswith("en") else zh
