from __future__ import annotations

import asyncio
import json
from typing import Any

from app.agent.commands import choose_context_tools, detect_append_request, detect_create_request, detect_replace_request
from app.agent.patch_flow import build_coder_patch_messages, parse_coder_patch, propose_append_patch, propose_coder_patch, propose_create_patch, propose_replace_patch
from app.agent.steps import AgentStep, allowed_tool_names, build_planner_messages, choose_rule_step, observation_seen, parse_agent_step
from app.agent.summary import build_model_messages, final_summary_text, model_purpose_for_mode
from app.agent.tool_flow import execute_tool, observe_tool
from app.agent.types import AgentRequest, AgentRuntime
from app.agent.utils import localized
from app.models.provider import ModelRequest
from app.sessions.store import Session


async def run_agent(session: Session, request: AgentRequest, runtime: AgentRuntime) -> None:
    plan_items = [
        {"id": "loop", "text": localized(request.language, "执行 Agent 工具循环", "Run agent tool loop"), "status": "pending"},
        {"id": "summary", "text": localized(request.language, "输出阶段性结果", "Return phase summary"), "status": "pending"},
    ]
    await session.events.put({"type": "plan.created", "items": plan_items})

    await asyncio.sleep(0.05)
    await session.events.put({"type": "plan.updated", "item_id": "loop", "status": "in_progress"})
    observations = await run_context_loop(session, request, runtime)
    append_request = detect_append_request(request.message)
    replace_request = detect_replace_request(request.message)
    create_request = detect_create_request(request.message)
    patch_outcome = None
    if append_request is not None:
        patch_outcome = await propose_append_patch(session, request, append_request[0], append_request[1], runtime)
    elif replace_request is not None:
        patch_outcome = await propose_replace_patch(session, request, replace_request[0], replace_request[1], replace_request[2], runtime)
    elif create_request is not None:
        patch_outcome = await propose_create_patch(session, request, create_request[0], create_request[1], runtime)
    elif should_attempt_coder_patch(request, runtime):
        patch_outcome = await propose_model_patch(session, request, observations, runtime)
    if patch_outcome is not None:
        observations.append(patch_outcome)
        rebuild_outcome = await maybe_rebuild_stale_patch(session, request, observations, patch_outcome, runtime)
        if rebuild_outcome is not None:
            observations.append(rebuild_outcome)
            patch_outcome = rebuild_outcome
        repair_outcome = await maybe_repair_failed_verification(session, request, observations, patch_outcome, runtime)
        if repair_outcome is not None:
            observations.append(repair_outcome)
    await session.events.put({"type": "plan.updated", "item_id": "loop", "status": "completed"})

    purpose = model_purpose_for_mode(request.mode)
    messages = build_model_messages(request, observations)
    await emit_context_budget_event(session, purpose, messages)
    response = await runtime.model_router.complete(
        ModelRequest(
            purpose=purpose,
            messages=messages,
            max_tokens=900 if purpose == "reviewer" else 500,
        )
    )
    await record_model_usage(session, response, purpose, runtime)
    await session.events.put({"type": "plan.updated", "item_id": "summary", "status": "completed"})
    final_summary = final_summary_text(request, observations, response.text, response.provider)
    runtime.audit.record("session.final", session_id=session.session_id, workspace=session.workspace, data={"mode": request.mode, "purpose": purpose})
    await session.events.put(
        {
            "type": "final",
            "summary": final_summary,
        }
    )


async def run_agent_safely(session: Session, request: AgentRequest, runtime: AgentRuntime) -> None:
    try:
        await run_agent(session, request, runtime)
    except Exception as exc:
        await emit_agent_failure(session, request, exc, runtime)


async def emit_agent_failure(session: Session, request: AgentRequest, exc: Exception, runtime: AgentRuntime) -> None:
    runtime.audit.record(
        "session.error",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "mode": request.mode,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        },
    )
    message = localized(
        request.language,
        f"Agent 执行失败: {exc}",
        f"Agent execution failed: {exc}",
    )
    await session.events.put(
        {
            "type": "error",
            "error": message,
            "error_type": exc.__class__.__name__,
        }
    )
    await session.events.put(
        {
            "type": "final",
            "summary": message,
        }
    )


async def run_context_loop(session: Session, request: AgentRequest, runtime: AgentRuntime, max_steps: int = 10) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    context_tools = choose_context_tools(request)

    for index in range(1, max_steps + 1):
        step = choose_rule_step(request.message, request.mode, observations, context_tools)
        if step.action == "finish":
            model_step = await choose_model_step(session, request, observations, runtime)
            if model_step is not None and should_use_model_step(model_step, observations):
                step = model_step

        await emit_agent_step(session, step, index, runtime)
        if step.action == "finish":
            break

        result = await execute_tool(session, request, step.tool, step.args, runtime, context=step.context)
        observations.append(observe_tool(step.tool, step.args, result, context=step.context))
    else:
        await session.events.put(
            {
                "type": "agent.loop.max_steps",
                "max_steps": max_steps,
                "message": localized(request.language, "Agent 工具循环达到步数上限。", "Agent tool loop reached the step limit."),
            }
        )

    return observations


async def choose_model_step(
    session: Session, request: AgentRequest, observations: list[dict[str, Any]], runtime: AgentRuntime
) -> AgentStep | None:
    if not primary_model_available(runtime):
        return None

    messages = build_planner_messages(
        language=request.language,
        message=request.message,
        mode=request.mode,
        workspace=request.workspace,
        observations=observations,
    )
    await emit_context_budget_event(session, "planner", messages)
    response = await runtime.model_router.complete(
        ModelRequest(
            purpose="planner",
            messages=messages,
            temperature=0,
            max_tokens=260,
        )
    )
    await record_model_usage(session, response, "planner", runtime)
    return parse_agent_step(response.text, allowed_tool_names(request.mode))


def primary_model_available(runtime: AgentRuntime) -> bool:
    is_configured = getattr(runtime.model_router.primary, "is_configured", None)
    return bool(is_configured()) if callable(is_configured) else True


def should_attempt_coder_patch(request: AgentRequest, runtime: AgentRuntime) -> bool:
    if request.mode == "review":
        return False
    if not primary_model_available(runtime):
        return False
    message = request.message.lower()
    write_intents = [
        "fix",
        "implement",
        "change",
        "update",
        "add",
        "write",
        "修复",
        "实现",
        "修改",
        "更新",
        "新增",
        "添加",
        "补",
    ]
    return any(token in message for token in write_intents)


async def propose_model_patch(
    session: Session,
    request: AgentRequest,
    observations: list[dict[str, Any]],
    runtime: AgentRuntime,
) -> dict[str, Any] | None:
    messages = build_coder_patch_messages(request, observations)
    await emit_context_budget_event(session, "coder", messages)
    response = await runtime.model_router.complete(
        ModelRequest(
            purpose="coder",
            messages=messages,
            temperature=0,
            max_tokens=1800,
        )
    )
    await record_model_usage(session, response, "coder", runtime)
    patch = parse_coder_patch(response.text)
    if patch is None:
        return None
    return await propose_coder_patch(session, request, patch, runtime)


async def maybe_repair_failed_verification(
    session: Session,
    request: AgentRequest,
    observations: list[dict[str, Any]],
    patch_outcome: dict[str, Any],
    runtime: AgentRuntime,
) -> dict[str, Any] | None:
    if not should_repair_failed_verification(request, patch_outcome, runtime):
        return None
    runtime.audit.record(
        "verification.repair.started",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"files": patch_outcome.get("files", []), "verification": patch_outcome.get("verification", {})},
    )
    await session.events.put(
        {
            "type": "verification.repair.started",
            "message": localized(request.language, "验证失败，尝试生成一次后续修复 patch。", "Verification failed; attempting one follow-up repair patch."),
        }
    )
    return await propose_model_patch(session, request, observations, runtime)


async def maybe_rebuild_stale_patch(
    session: Session,
    request: AgentRequest,
    observations: list[dict[str, Any]],
    patch_outcome: dict[str, Any],
    runtime: AgentRuntime,
) -> dict[str, Any] | None:
    if not should_rebuild_stale_patch(request, patch_outcome, runtime):
        return None
    runtime.audit.record(
        "patch.rebuild.started",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"files": patch_outcome.get("files", []), "reason": patch_outcome.get("reason", "")},
    )
    await session.events.put(
        {
            "type": "patch.rebuild.started",
            "files": patch_outcome.get("files", []),
            "reason": patch_outcome.get("reason", ""),
            "message": localized(request.language, "Patch 已过期，尝试重新生成一次 diff。", "Patch is stale; attempting to rebuild the diff once."),
        }
    )
    return await propose_model_patch(session, request, observations, runtime)


def should_rebuild_stale_patch(request: AgentRequest, patch_outcome: dict[str, Any], runtime: AgentRuntime) -> bool:
    if request.mode == "review" or not primary_model_available(runtime):
        return False
    return patch_outcome.get("status") == "stale"


def should_repair_failed_verification(request: AgentRequest, patch_outcome: dict[str, Any], runtime: AgentRuntime) -> bool:
    if request.mode == "review" or not primary_model_available(runtime):
        return False
    if patch_outcome.get("status") != "applied":
        return False
    verification = patch_outcome.get("verification")
    if not isinstance(verification, dict):
        return False
    return verification.get("status") == "failed"


def should_use_model_step(step: AgentStep, observations: list[dict[str, Any]]) -> bool:
    if step.action == "finish":
        return True
    return not observation_seen(observations, step.tool, step.args)


async def emit_agent_step(session: Session, step: AgentStep, index: int, runtime: AgentRuntime) -> None:
    payload = step.to_dict()
    runtime.audit.record(
        "agent.step",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"index": index, **payload},
    )
    await session.events.put({"type": "agent.step", "index": index, **payload})


async def record_model_usage(session: Session, response: Any, purpose: str, runtime: AgentRuntime) -> None:
    runtime.audit.record(
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


async def emit_context_budget_event(session: Session, purpose: str, messages: list[dict[str, str]]) -> None:
    if len(messages) < 2:
        return
    try:
        payload = json.loads(messages[1].get("content") or "{}")
    except json.JSONDecodeError:
        return
    context_budget = payload.get("context_budget")
    if not isinstance(context_budget, dict) or not context_budget.get("compacted"):
        return
    await session.events.put(
        {
            "type": "context.budget",
            "purpose": purpose,
            **context_budget,
        }
    )
