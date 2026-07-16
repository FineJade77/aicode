from __future__ import annotations

from typing import Any

from app.agent.steps import step_key
from app.agent.types import AgentRequest, AgentRuntime
from app.agent.utils import compact_tool_data, localized, truncate_for_model
from app.audit.logger import stable_hash
from app.sessions.store import Session
from app.tools.base import ToolResult


async def execute_tool(session: Session, request: AgentRequest, name: str, args: dict[str, Any], runtime: AgentRuntime) -> ToolResult:
    decision = runtime.tools.evaluate(name, args, mode=request.mode)
    runtime.audit.record(
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

    if not decision.allowed and runtime.tools.is_approvable(name, decision):
        accepted, approval_id = await request_tool_approval(session, request, name, args, decision, runtime)
        if accepted is not True:
            reason = localized(request.language, "等待工具确认超时", "tool approval timed out")
            if accepted is False:
                reason = localized(request.language, "用户拒绝工具执行", "user rejected the tool")
            return await emit_tool_rejected(session, name, args, reason, decision.risk_level, approval_id, runtime)
        result = await runtime.tools.run_after_approval(name, args, workspace=request.workspace, mode=request.mode, language=request.language)
    else:
        result = await runtime.tools.run(name, args, workspace=request.workspace, mode=request.mode, language=request.language)

    return await emit_tool_result(session, name, result, runtime)


async def request_tool_approval(
    session: Session,
    request: AgentRequest,
    name: str,
    args: dict[str, Any],
    decision: Any,
    runtime: AgentRuntime,
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
    runtime.audit.record(
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


async def emit_tool_result(session: Session, name: str, result: ToolResult, runtime: AgentRuntime) -> ToolResult:
    if result.success:
        runtime.audit.record(
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
    runtime.audit.record(
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
    runtime: AgentRuntime,
) -> ToolResult:
    result = ToolResult(
        success=False,
        error=reason,
        risk_level=risk_level,
        requires_approval=True,
        data={"approval_id": approval_id, "args": args},
    )
    runtime.audit.record(
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
