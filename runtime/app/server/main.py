from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.steps import AgentStep, allowed_tool_names, build_planner_messages, choose_rule_step, observation_seen, parse_agent_step, step_key
from app.audit.logger import AuditLogger, stable_hash
from app.config.settings import settings
from app.events.sse import encode_sse
from app.models.provider import ModelRequest
from app.models.router import ModelRouter
from app.project.config import load_project_config
from app.sessions.store import Session, store
from app.tools.base import ToolError, ToolResult
from app.tools.patch import apply_content_patch, create_append_patch
from app.tools.review import review_rules_data
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
    store.append_message(session, request.model_dump())
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


async def run_agent(session: Session, request: MessageRequest) -> None:
    plan_items = [
        {"id": "loop", "text": localized(request.language, "执行 Agent 工具循环", "Run agent tool loop"), "status": "pending"},
        {"id": "summary", "text": localized(request.language, "输出阶段性结果", "Return phase summary"), "status": "pending"},
    ]
    await session.events.put({"type": "plan.created", "items": plan_items})

    await asyncio.sleep(0.05)
    await session.events.put({"type": "plan.updated", "item_id": "loop", "status": "in_progress"})
    observations = await run_context_loop(session, request)
    append_request = detect_append_request(request.message)
    if append_request is not None:
        await propose_append_patch(session, request, append_request[0], append_request[1])
    await session.events.put({"type": "plan.updated", "item_id": "loop", "status": "completed"})

    purpose = model_purpose_for_mode(request.mode)
    response = await model_router.complete(
        ModelRequest(
            purpose=purpose,
            messages=build_model_messages(request, observations),
            max_tokens=900 if purpose == "reviewer" else 500,
        )
    )
    await record_model_usage(session, response, purpose)
    await session.events.put({"type": "plan.updated", "item_id": "summary", "status": "completed"})
    final_summary = final_summary_text(request, observations, response.text, response.provider)
    audit.record("session.final", session_id=session.session_id, workspace=session.workspace, data={"mode": request.mode, "purpose": purpose})
    await session.events.put(
        {
            "type": "final",
            "summary": final_summary,
        }
    )


async def run_context_loop(session: Session, request: MessageRequest, max_steps: int = 8) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    context_tools = choose_context_tools(request)

    for index in range(1, max_steps + 1):
        step = choose_rule_step(request.message, request.mode, observations, context_tools)
        if step.action == "finish":
            model_step = await choose_model_step(session, request, observations)
            if model_step is not None and should_use_model_step(model_step, observations):
                step = model_step

        await emit_agent_step(session, step, index)
        if step.action == "finish":
            break

        result = await execute_tool(session, request, step.tool, step.args)
        observations.append(observe_tool(step.tool, step.args, result))
    else:
        await session.events.put(
            {
                "type": "agent.loop.max_steps",
                "max_steps": max_steps,
                "message": localized(request.language, "Agent 工具循环达到步数上限。", "Agent tool loop reached the step limit."),
            }
        )

    return observations


async def choose_model_step(session: Session, request: MessageRequest, observations: list[dict[str, Any]]) -> AgentStep | None:
    if not planner_model_available():
        return None

    response = await model_router.complete(
        ModelRequest(
            purpose="planner",
            messages=build_planner_messages(
                language=request.language,
                message=request.message,
                mode=request.mode,
                workspace=request.workspace,
                observations=observations,
            ),
            temperature=0,
            max_tokens=260,
        )
    )
    await record_model_usage(session, response, "planner")
    return parse_agent_step(response.text, allowed_tool_names(request.mode))


def planner_model_available() -> bool:
    is_configured = getattr(model_router.primary, "is_configured", None)
    return bool(is_configured()) if callable(is_configured) else True


def should_use_model_step(step: AgentStep, observations: list[dict[str, Any]]) -> bool:
    if step.action == "finish":
        return True
    return not observation_seen(observations, step.tool, step.args)


async def emit_agent_step(session: Session, step: AgentStep, index: int) -> None:
    payload = step.to_dict()
    audit.record(
        "agent.step",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"index": index, **payload},
    )
    await session.events.put({"type": "agent.step", "index": index, **payload})


async def record_model_usage(session: Session, response, purpose: str) -> None:
    audit.record(
        "usage.recorded",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "model": response.model,
            "provider": response.provider,
            "purpose": purpose,
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
            "purpose": purpose,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "estimated_cost": response.estimated_cost,
        }
    )


async def execute_tool(session: Session, request: MessageRequest, name: str, args: dict[str, Any]) -> ToolResult:
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

    if not decision.allowed and tools.is_approvable(name, decision):
        accepted, approval_id = await request_tool_approval(session, request, name, args, decision)
        if accepted is not True:
            reason = localized(request.language, "等待工具确认超时", "tool approval timed out")
            if accepted is False:
                reason = localized(request.language, "用户拒绝工具执行", "user rejected the tool")
            return await emit_tool_rejected(session, name, args, reason, decision.risk_level, approval_id)
        result = await tools.run_after_approval(name, args, workspace=request.workspace, mode=request.mode, language=request.language)
    else:
        result = await tools.run(name, args, workspace=request.workspace, mode=request.mode, language=request.language)

    return await emit_tool_result(session, name, result)


async def request_tool_approval(
    session: Session,
    request: MessageRequest,
    name: str,
    args: dict[str, Any],
    decision,
) -> tuple[bool | None, str]:
    approval = session.create_approval(
        "tool",
        {
            "tool": name,
            "args": args,
            "risk_level": decision.risk_level,
            "reason": decision.reason,
        },
    )
    message = localized(
        request.language,
        f"是否允许执行工具 {name}: {args}？原因: {decision.reason}",
        f"Allow tool {name}: {args}? Reason: {decision.reason}",
    )
    audit.record(
        "approval.requested",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "approval_id": approval.approval_id,
            "kind": "tool",
            "tool": name,
            "args": args,
            "risk_level": decision.risk_level,
            "reason": decision.reason,
        },
    )
    await session.events.put(
        {
            "type": "approval.requested",
            "approval_id": approval.approval_id,
            "kind": "tool",
            "tool": name,
            "args": args,
            "risk_level": decision.risk_level,
            "message": message,
        }
    )
    return await session.wait_for_approval(approval.approval_id), approval.approval_id


async def emit_tool_result(session: Session, name: str, result: ToolResult) -> ToolResult:
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
        return result

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
    return result


async def emit_tool_rejected(
    session: Session,
    name: str,
    args: dict[str, Any],
    reason: str,
    risk_level: str,
    approval_id: str,
) -> ToolResult:
    result = ToolResult(
        success=False,
        error=reason,
        risk_level=risk_level,
        requires_approval=True,
        data={"approval_id": approval_id, "args": args},
    )
    audit.record(
        "tool.rejected",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "tool": name,
            "error": reason,
            "risk_level": risk_level,
            "requires_approval": True,
            "approval_id": approval_id,
            "args": args,
        },
    )
    await session.events.put(
        {
            "type": "tool.rejected",
            "tool": name,
            "error": reason,
            "risk_level": risk_level,
            "requires_approval": True,
            "approval_id": approval_id,
        }
    )
    return result


def observe_tool(name: str, args: dict[str, Any], result: ToolResult) -> dict[str, Any]:
    return {
        "tool": name,
        "args": args,
        "step_key": step_key(name, args),
        "success": result.success,
        "risk_level": result.risk_level,
        "requires_approval": result.requires_approval,
        "text": truncate_for_model(result.text or result.error),
        "data": compact_tool_data(result.data),
    }


def compact_tool_data(data: dict[str, Any]) -> dict[str, Any]:
    if not data:
        return {}
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) <= 12_000:
        return data
    return {"truncated": True, "preview": encoded[:12_000]}


def truncate_for_model(text: str, limit: int = 12_000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[TRUNCATED]"


def model_purpose_for_mode(mode: str) -> str:
    if mode == "review":
        return "reviewer"
    return "summarizer"


def build_model_messages(request: MessageRequest, observations: list[dict[str, Any]]) -> list[dict[str, str]]:
    language_name = "English" if request.language.startswith("en") else "中文"
    if request.mode == "review":
        system = (
            f"你是 aicode 的只读代码审查助手。使用{language_name}回答。"
            "只基于工具输出做结论，不要编造没有证据的问题。"
            "不得建议已经修改代码；review 模式只允许只读分析。"
            "优先输出: 结论、必须处理的问题、可选改进、建议验证命令。"
        )
    else:
        system = (
            f"你是 aicode 的 coding agent 摘要助手。使用{language_name}回答。"
            "基于工具输出给出简洁进展总结和下一步建议，不要编造。"
        )

    payload = {
        "user_request": request.message,
        "mode": request.mode,
        "workspace": request.workspace,
        "tool_observations": observations,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def final_summary_text(request: MessageRequest, observations: list[dict[str, Any]], model_text: str, provider: str) -> str:
    cleaned = model_text.strip()
    if provider != "stub" and cleaned:
        return cleaned

    if request.mode == "review":
        prefix = localized(
            request.language,
            "模型 provider 未配置，以下为确定性 Review 结果：",
            "Model provider is not configured. Deterministic review result:",
        )
        return prefix + "\n\n" + format_review_fallback_summary(request.language, observations)

    if provider == "stub" or not cleaned:
        return localized(
            request.language,
            "Agent 工具循环已连通。Runtime 已通过结构化步骤读取工作区、检查 git 状态，并支持写入前 inline diff 确认。",
            "Agent tool loop is connected. The runtime used structured steps to inspect the workspace, check git status, and supports inline diff approval before writes.",
        )
    return cleaned


def review_observation_text(observations: list[dict[str, Any]]) -> str:
    for observation in observations:
        if observation.get("tool") == "review_diff" and observation.get("success"):
            text = str(observation.get("text") or "").strip()
            if text:
                return text
    return "Review 未产生可用结果。"


def format_review_fallback_summary(language: str, observations: list[dict[str, Any]]) -> str:
    review = find_observation(observations, "review_diff")
    if not review or not review.get("success"):
        return localized(language, "结论\nReview 未产生可用结果。", "Conclusion\nReview did not produce a usable result.")

    data = review.get("data") if isinstance(review.get("data"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    findings = data.get("findings") if isinstance(data.get("findings"), list) else []
    finding_count = int_value(summary.get("finding_count"), len(findings))
    severity = summary.get("by_severity") if isinstance(summary.get("by_severity"), dict) else {}
    high = int_value(severity.get("high"), 0)
    medium = int_value(severity.get("medium"), 0)
    low = int_value(severity.get("low"), 0)

    if language.startswith("en"):
        return format_review_fallback_summary_en(observations, findings, finding_count, high, medium, low)
    return format_review_fallback_summary_zh(observations, findings, finding_count, high, medium, low)


def format_review_fallback_summary_zh(
    observations: list[dict[str, Any]], findings: list[Any], finding_count: int, high: int, medium: int, low: int
) -> str:
    lines: list[str] = ["结论"]
    if finding_count == 0:
        lines.append("未发现确定性风险。")
    else:
        lines.append(f"发现 {finding_count} 个确定性问题：high={high} medium={medium} low={low}。")

    lines.extend(["", "风险"])
    if finding_count == 0:
        lines.append("- 无必须处理问题。")
    else:
        for finding in normalized_findings(findings)[:8]:
            lines.append(f"- [{finding['severity']}] {finding['location']} {finding['title']}：{finding['message']}")
        if finding_count > 8:
            lines.append(f"- 其余 {finding_count - 8} 个问题已省略，请查看上方 review_diff 输出。")

    lines.extend(["", "建议验证"])
    test_command = detected_test_command(observations)
    if test_command:
        lines.append(f"- 运行 `{test_command}`。")
    else:
        lines.append("- 运行项目测试。")
    lines.append("- 修复后重新运行 `aicode review`。")
    return "\n".join(lines)


def format_review_fallback_summary_en(
    observations: list[dict[str, Any]], findings: list[Any], finding_count: int, high: int, medium: int, low: int
) -> str:
    lines: list[str] = ["Conclusion"]
    if finding_count == 0:
        lines.append("No deterministic risks found.")
    else:
        lines.append(f"Found {finding_count} deterministic issues: high={high} medium={medium} low={low}.")

    lines.extend(["", "Risks"])
    if finding_count == 0:
        lines.append("- No must-fix issues.")
    else:
        for finding in normalized_findings(findings)[:8]:
            lines.append(f"- [{finding['severity']}] {finding['location']} {finding['title']}: {finding['message']}")
        if finding_count > 8:
            lines.append(f"- {finding_count - 8} more findings omitted; see the review_diff output above.")

    lines.extend(["", "Suggested Verification"])
    test_command = detected_test_command(observations)
    if test_command:
        lines.append(f"- Run `{test_command}`.")
    else:
        lines.append("- Run the project test suite.")
    lines.append("- Run `aicode review` again after fixes.")
    return "\n".join(lines)


def normalized_findings(findings: list[Any]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for raw in findings:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or ".")
        line = raw.get("line")
        location = path + (f":{line}" if line is not None else "")
        normalized.append(
            {
                "severity": str(raw.get("severity") or "low"),
                "location": location,
                "title": str(raw.get("title") or "问题"),
                "message": str(raw.get("message") or ""),
            }
        )
    return normalized


def detected_test_command(observations: list[dict[str, Any]]) -> str | None:
    project = find_observation(observations, "detect_project")
    if not project or not isinstance(project.get("data"), dict):
        return None
    command = project["data"].get("test_command")
    if not command:
        return None
    return str(command)


def find_observation(observations: list[dict[str, Any]], tool: str) -> dict[str, Any] | None:
    for observation in observations:
        if observation.get("tool") == tool:
            return observation
    return None


def int_value(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def choose_context_tools(request: MessageRequest) -> list[tuple[str, dict[str, Any]]]:
    message = request.message.strip()
    lowered = message.lower()

    if detect_append_request(message) is not None:
        return []

    shell_command = detect_shell_request(message)
    if shell_command and request.mode != "review":
        return [("run_shell", {"command": shell_command, "timeout": 120})]

    if request.mode == "review" or "审查" in message:
        return [("review_diff", {})]

    if request.mode == "diff" or "diff" in lowered or "变更" in message:
        return [("git_diff", {})]

    if request.mode == "test" or "运行测试" in message or "run tests" in lowered:
        return [("run_tests", {"timeout": 120})]

    target = extract_target(message)
    if target:
        return [("read_file", {"path": target, "max_bytes": 30_000})]

    keyword = extract_keyword(message)
    if keyword:
        return [("search_text", {"query": keyword, "limit": 40})]

    return []


def detect_shell_request(message: str) -> str | None:
    stripped = message.strip()
    lowered = stripped.lower()
    prefixes = ["shell ", "run shell ", "运行命令 ", "执行命令 "]
    for prefix in prefixes:
        if lowered.startswith(prefix) or stripped.startswith(prefix):
            command = stripped[len(prefix) :].strip()
            return command or None
    return None


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
        project_config = load_project_config(Path(request.workspace))
        proposal = create_append_patch(Path(request.workspace), path, text, protected_paths=project_config.protected_paths)
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
        project_config = load_project_config(Path(request.workspace))
        apply_content_patch(Path(request.workspace), proposal.path, proposal.new_content, protected_paths=project_config.protected_paths)
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


def datetime_utc_today():
    return datetime.now(timezone.utc).date()


def extract_keyword(message: str) -> str | None:
    for token in ["login", "auth", "test", "pytest", "go test", "错误", "失败", "测试", "登录", "认证"]:
        if token in message.lower() or token in message:
            return token
    return None


def localized(language: str, zh: str, en: str) -> str:
    return en if language.startswith("en") else zh
