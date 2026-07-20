from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.agent.history import compact_if_needed, load_history, persist_message, truncate_tool_output
from app.agent.prompts import BUDGET_NOTE_EN, BUDGET_NOTE_ZH, VERIFY_NOTE_EN, VERIFY_NOTE_ZH, build_system_prompt
from app.agent.turn import TurnBudget, assistant_message, tool_message, user_message, user_note
from app.agent.utils import localized
from app.models.provider import (
    TOOL_ARGUMENT_PARSE_ERROR_KEY,
    CompletionResult,
    ToolCallRequest,
    tool_argument_parse_error,
)
from app.policy.engine import PolicyEngine
from app.sessions.store import Session
from app.tools.base import ToolResult, is_protected_path
from app.tools.edit import EditError, EditStaleError, apply_edit, build_edit_proposal
from app.tools.registry import build_tool_context, run_tool, tool_schemas_for_mode, validate_tool_arguments


async def run_turn_safely(session: Session, request: Any, runtime: Any) -> None:
    try:
        await run_turn(session, request, runtime)
    except Exception as exc:
        runtime.audit.record(
            "session.error",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"mode": request.mode, "error_type": exc.__class__.__name__, "error": str(exc)},
        )
        message = localized(request.language, f"Agent 执行失败: {exc}", f"Agent execution failed: {exc}")
        await session.events.put({"type": "error", "error": message, "error_type": exc.__class__.__name__})
        await session.events.put({"type": "final", "summary": message})


async def run_turn(session: Session, request: Any, runtime: Any) -> None:
    policy: PolicyEngine = runtime.policy or PolicyEngine()
    system = build_system_prompt(request)
    history = load_history(session)
    # Persist only the run that is starting, so queued future prompts do not leak into this history.
    current_user_message = user_message(str(request.message))
    history.append(current_user_message)
    persist_message(session, current_user_message)
    tools = tool_schemas_for_mode(request.mode)
    context = build_tool_context(request.workspace, request.mode, request.language)
    purpose = "reviewer" if request.mode == "review" else "main"
    budget = TurnBudget()
    applied_edits = 0
    verify_note_sent = False
    result: CompletionResult | None = None

    async def on_delta(text: str) -> None:
        await session.events.put({"type": "assistant.delta", "text": text})

    for _step in range(budget.max_steps):
        result = await runtime.model_router.stream_complete(
            purpose=purpose, system=system, messages=history, tools=tools,
            on_text_delta=on_delta, max_tokens=budget.max_tokens_per_call,
        )
        await record_usage(session, result, purpose, runtime)
        message = assistant_message(result)
        history.append(message)
        persist_message(session, message)

        if not result.tool_calls:
            if applied_edits > 0 and not verify_note_sent:
                verify_note_sent = True
                note = user_note(localized(request.language, VERIFY_NOTE_ZH, VERIFY_NOTE_EN))
                history.append(note)
                persist_message(session, note)
                continue
            break

        for call in result.tool_calls:
            output, applied = await execute_gated(session, request, call, runtime, policy, context)
            applied_edits += applied
            reply = tool_message(call.id, output)
            history.append(reply)
            persist_message(session, reply)

        history = await compact_if_needed(history, runtime, session)
    else:
        note = user_note(localized(request.language, BUDGET_NOTE_ZH, BUDGET_NOTE_EN))
        history.append(note)
        persist_message(session, note)
        result = await runtime.model_router.stream_complete(
            purpose=purpose, system=system, messages=history, tools=[], on_text_delta=on_delta,
        )
        await record_usage(session, result, purpose, runtime)
        message = assistant_message(result)
        history.append(message)
        persist_message(session, message)

    summary = result.text if result is not None else ""
    runtime.audit.record("session.final", session_id=session.session_id, workspace=session.workspace, data={"mode": request.mode})
    await session.events.put({"type": "final", "summary": summary})


async def execute_gated(
    session: Session, request: Any, call: ToolCallRequest, runtime: Any, policy: PolicyEngine, context: Any
) -> tuple[str, int]:
    parse_error = None
    if not isinstance(call.arguments, dict):
        parse_error_payload = tool_argument_parse_error(repr(call.arguments), ValueError("tool arguments JSON must be an object"))
        parse_error = parse_error_payload[TOOL_ARGUMENT_PARSE_ERROR_KEY]
    elif TOOL_ARGUMENT_PARSE_ERROR_KEY in call.arguments:
        parse_error = call.arguments[TOOL_ARGUMENT_PARSE_ERROR_KEY]
    if parse_error is not None:
        detail = parse_error.get("error", "") if isinstance(parse_error, dict) else str(parse_error)
        message = localized(
            request.language,
            f"工具 {call.name} 的参数不是合法 JSON，已跳过执行。请重新生成合法 JSON 参数后再调用该工具。{detail}",
            f"Tool {call.name} received invalid JSON arguments and was not executed. Regenerate valid JSON arguments before calling it again. {detail}",
        ).strip()
        runtime.audit.record(
            "tool.argument_parse_error",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"tool": call.name, "parse_error": parse_error},
        )
        await session.events.put(tool_event("tool.error", call.name, error=message, data={"parse_error": parse_error}, duration_ms=0))
        return f"[工具参数解析失败] {message}", 0

    validation_error = validate_tool_arguments(call.name, call.arguments)
    if validation_error is not None:
        message = localized(
            request.language,
            f"工具 {call.name} 参数校验失败，已跳过执行。请按工具 schema 重新生成参数。{validation_error}",
            f"Tool {call.name} arguments failed validation and were not executed. Regenerate arguments that match the tool schema. {validation_error}",
        ).strip()
        runtime.audit.record(
            "tool.argument_validation_error",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"tool": call.name, "validation_error": validation_error, "args": call.arguments},
        )
        await session.events.put(tool_event("tool.error", call.name, error=message, data={"validation_error": validation_error}, duration_ms=0))
        return f"[工具参数校验失败] {message}", 0

    gate = policy.gate(call.name, call.arguments, mode=request.mode, language=request.language)
    runtime.audit.record(
        "tool.started",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"tool": call.name, "args": call.arguments, "verdict": gate.verdict, "risk_level": gate.risk_level},
    )
    await session.events.put({"type": "tool.started", "tool": call.name, "args": call.arguments, "risk_level": gate.risk_level})

    if gate.verdict == "deny":
        await session.events.put({"type": "tool.denied", "tool": call.name, "error": gate.reason, "risk_level": gate.risk_level})
        return f"[被策略拒绝] {gate.reason}", 0

    if call.name == "edit_file":
        return await execute_edit(session, request, call, runtime, context)

    if gate.verdict == "ask":
        accepted = await request_approval(session, request, "tool", {"tool": call.name, "args": call.arguments, "reason": gate.reason})
        if accepted is not True:
            reason = localized(request.language, "用户拒绝执行该命令", "user rejected the command")
            await session.events.put({"type": "tool.rejected", "tool": call.name, "error": reason, "risk_level": gate.risk_level})
            return f"[{reason}]", 0

    result = await run_tool(call.name, call.arguments, context)
    if result.success:
        output = truncate_tool_output(call.name, result.text)
        runtime.audit.record(
            "tool.finished",
            session_id=session.session_id,
            workspace=session.workspace,
            data=tool_finish_audit_data(call.name, result),
        )
        await session.events.put(
            tool_event("tool.output", call.name, text=result.text[:2000], data=result.data, duration_ms=result.duration_ms)
        )
        return output, 0
    runtime.audit.record(
        "tool.finished",
        session_id=session.session_id,
        workspace=session.workspace,
        data=tool_finish_audit_data(call.name, result),
    )
    await session.events.put(tool_event("tool.error", call.name, error=result.error, data=result.data, duration_ms=result.duration_ms))
    return f"[错误] {truncate_tool_output(call.name, result.error)}", 0


async def execute_edit(session: Session, request: Any, call: ToolCallRequest, runtime: Any, context: Any) -> tuple[str, int]:
    started = time.perf_counter()
    workspace = Path(request.workspace)
    try:
        proposal = build_edit_proposal(workspace, call.arguments, context.protected_paths)
    except EditError as exc:
        await session.events.put(tool_event("tool.error", "edit_file", error=str(exc), duration_ms=elapsed_ms(started)))
        return f"[编辑失败] {exc}", 0

    auto = session.auto_accept_edits and not is_protected_path(proposal.path, context.protected_paths)
    if auto:
        await session.events.put({"type": "edit.auto_approved", "path": proposal.path})
        accepted = True
    else:
        accepted = await request_approval(
            session,
            request,
            "edit",
            {"path": proposal.path, "kind": proposal.kind, "diff": proposal.diff},
        )

    if accepted is not True:
        reason = localized(request.language, "用户拒绝了此编辑", "user rejected this edit")
        await session.events.put({"type": "edit.rejected", "path": proposal.path})
        return f"[{reason}] {proposal.path}", 0

    try:
        apply_edit(workspace, proposal)
    except EditStaleError as exc:
        await session.events.put(tool_event("tool.error", "edit_file", error=str(exc), duration_ms=elapsed_ms(started)))
        return f"[编辑失败·stale] {exc}", 0
    duration_ms = elapsed_ms(started)
    runtime.audit.record(
        "edit.applied",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"path": proposal.path, "kind": proposal.kind, "diff_bytes": len(proposal.diff), "duration_ms": duration_ms},
    )
    await session.events.put({"type": "edit.applied", "path": proposal.path, "kind": proposal.kind, "duration_ms": duration_ms})
    return f"已应用编辑 {proposal.path}:\n{proposal.diff}", 1


def tool_event(
    event_type: str,
    tool: str,
    *,
    text: str = "",
    error: str = "",
    data: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    payload = dict(data or {})
    event: dict[str, Any] = {"type": event_type, "tool": tool, "data": payload}
    if text:
        event["text"] = text
    if error:
        event["error"] = error
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    event.update(tool_observability_fields(payload))
    return event


def tool_observability_fields(data: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key in ("exit_code", "timed_out", "validation_error", "parse_error"):
        if key in data:
            fields[key] = data[key]
    return fields


def tool_finish_audit_data(tool: str, result: ToolResult) -> dict[str, Any]:
    data = {"tool": tool, "success": result.success, "duration_ms": result.duration_ms, "risk_level": result.risk_level}
    data.update(tool_observability_fields(result.data))
    return data


def elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


async def request_approval(session: Session, request: Any, kind: str, payload: dict[str, Any]) -> bool | None:
    approval = session.create_approval(kind, payload)
    message = localized(request.language, "等待用户确认", "waiting for user approval")
    await session.events.put({"type": "approval.requested", "approval_id": approval.approval_id, "kind": kind, "message": message, **payload})
    return await session.wait_for_approval(approval.approval_id)


async def record_usage(session: Session, result: CompletionResult, purpose: str, runtime: Any) -> None:
    payload = {
        "model": result.model,
        "provider": result.provider,
        "purpose": purpose,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "estimated_cost": result.estimated_cost,
    }
    runtime.audit.record("usage.recorded", session_id=session.session_id, workspace=session.workspace, data=payload)
    await session.events.put({"type": "usage.recorded", **payload})
