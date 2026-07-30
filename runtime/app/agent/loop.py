from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent.history import ContextManager, persist_message, truncate_tool_output
from app.agent.prompts import VERIFY_NOTE, budget_note, build_system_prompt
from app.agent.turn import TurnBudget, TurnLedger, assistant_message, tool_message, user_message, user_note
from app.agent.types import AgentRequest, AgentRuntime
from app.core.hashing import stable_hash
from app.core.session import AgentSession, ApprovalDecision
from app.models.provider import (
    TOOL_ARGUMENT_PARSE_ERROR_KEY,
    CompletionResult,
    ContextOverflowError,
    ToolCallRequest,
    tool_argument_parse_error,
)
from app.policy.engine import PolicyEngine


class AgentLoop:
    """Transport-independent orchestration for a single agent turn."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self.runtime = runtime
        if self.runtime.context_manager is None:
            self.runtime.context_manager = ContextManager(runtime)

    async def run(self, session: AgentSession, request: AgentRequest) -> None:
        await run_turn_safely(session, request, self.runtime)


async def run_turn_safely(session: AgentSession, request: AgentRequest, runtime: AgentRuntime) -> None:
    try:
        await run_turn(session, request, runtime)
    except Exception as exc:
        if runtime.trace is not None:
            runtime.trace.record(
                "session.error",
                session_id=session.session_id,
                workspace=session.workspace,
                data={"mode": request.mode, "error_type": exc.__class__.__name__, "error": str(exc)},
            )
        message = f"Agent execution failed: {exc}"
        await session.events.put({"type": "error", "error": message, "error_type": exc.__class__.__name__})
        await session.events.put({"type": "final", "summary": message})


async def run_turn(session: AgentSession, request: AgentRequest, runtime: AgentRuntime) -> None:
    policy: PolicyEngine = runtime.policy or PolicyEngine()
    if (
        runtime.workspace is None
        or runtime.tools is None
        or runtime.model_runtime is None
        or runtime.approvals is None
    ):
        raise RuntimeError("AgentRuntime is missing a workspace, tools, model, or approval runtime adapter")
    trust_status = (
        runtime.trust.status(Path(request.workspace))
        if runtime.trust is not None
        else {"level": "trusted"}
    )
    # Trust is resolved first: it decides whether Agent shell commands run on the
    # host or in the sandbox, and the system prompt has to state which one so the
    # model does not plan around capabilities it will not have.
    system = build_system_prompt(
        request,
        runtime.workspace.prompt_context(Path(request.workspace), trust_level=str(trust_status["level"])),
    )
    # Persist only the run that is starting, so queued future prompts do not leak
    # into this history. There is no in-memory transcript here on purpose: the
    # prompt is rebuilt from the session by ContextManager before every model
    # call, so a second local copy could only drift out of sync.
    persist_message(session, user_message(str(request.message)))
    tools = runtime.tools.schemas_for_mode(request.mode)
    context = runtime.tools.build_context(
        request.workspace,
        request.mode,
        execution=runtime.execution,
        session_id=session.session_id,
        run_id=session.current_run_id or "",
        trust_level=str(trust_status["level"]),
    )
    purpose = "reviewer" if request.mode == "review" else "main"
    model = str(getattr(request, "model", "") or "").strip() or None
    budget = turn_budget(runtime)
    ledger = TurnLedger()
    applied_edits = 0
    verify_note_sent = False
    result: CompletionResult | None = None

    async def on_delta(text: str) -> None:
        session.mark_agent_progress("model.stream")
        await session.events.put({"type": "assistant.delta", "text": text})

    budget_reason: str | None = None
    for _step in range(budget.max_steps):
        await apply_pending_steers(session, request, runtime)
        session.mark_agent_progress("model.request")
        _prompt, result = await complete_with_compaction(
            session=session,
            runtime=runtime,
            purpose=purpose,
            model=model,
            system=system,
            tools=tools,
            on_text_delta=on_delta,
            max_tokens=budget.max_tokens_per_call,
        )
        ledger.add(result)
        await record_usage(session, result, purpose, runtime)
        message = assistant_message(result)
        persist_message(session, message)

        if await apply_pending_steers(session, request, runtime, result.tool_calls):
            continue

        budget_reason = ledger.exceeded(budget)
        if budget_reason is not None:
            await emit_budget_exceeded(session, runtime, budget, ledger, budget_reason)
            break

        if not result.tool_calls:
            if applied_edits > 0 and not verify_note_sent:
                verify_note_sent = True
                persist_message(session, user_note(VERIFY_NOTE))
                continue
            session.mark_agent_progress("finalizing")
            break

        for call in result.tool_calls:
            session.mark_agent_progress(f"tool.{call.name}")
            output, applied = await execute_gated(session, request, call, runtime, policy, context)
            applied_edits += applied
            persist_message(session, tool_message(call.id, output))

    else:
        budget_reason = "steps"
        await emit_budget_exceeded(session, runtime, budget, ledger, budget_reason)

    if budget_reason is not None:
        # One shared wind-down for every exhausted budget dimension, so the user
        # always receives a summary rather than a truncated transcript. This call
        # is outside the loop and its usage is never re-gated, which is what stops
        # the wind-down from recursing into another budget stop.
        persist_message(session, user_note(budget_note(budget_reason)))
        session.mark_agent_progress("model.request")
        _prompt, result = await complete_with_compaction(
            session=session,
            runtime=runtime,
            purpose=purpose,
            model=model,
            system=system,
            tools=[],
            on_text_delta=on_delta,
            max_tokens=budget.max_tokens_per_call,
        )
        ledger.add(result)
        await record_usage(session, result, purpose, runtime)
        message = assistant_message(result)
        persist_message(session, message)
        session.mark_agent_progress("finalizing")

    summary = str(message.get("content") or "") if result is not None else ""
    if runtime.trace is not None:
        runtime.trace.record(
            "session.final",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"mode": request.mode},
        )
    await session.events.put({"type": "final", "summary": summary})


async def complete_with_compaction(
    *,
    session: AgentSession,
    runtime: AgentRuntime,
    purpose: str,
    model: str | None = None,
    system: str,
    tools: list[dict[str, Any]] | tuple[Any, ...],
    on_text_delta: Any,
    max_tokens: int,
) -> tuple[list[dict[str, Any]], CompletionResult]:
    context_manager = runtime.context_manager or ContextManager(runtime)
    history = await context_manager.prepare(
        session=session,
        purpose=purpose,
        model=model,
        system=system,
        tools=tools,
        max_tokens=max_tokens,
    )
    try:
        assert runtime.model_runtime is not None
        completion_args = {
            "purpose": purpose,
            "system": system,
            "messages": history,
            "tools": tools,
            "on_text_delta": on_text_delta,
            "max_tokens": max_tokens,
        }
        if model is not None:
            completion_args["model"] = model
        result = await runtime.model_runtime.stream_complete(
            **completion_args,
        )
    except ContextOverflowError:
        session.mark_agent_progress("context.compaction_retry")
        history = await context_manager.prepare(
            session=session,
            purpose=purpose,
            model=model,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            force=True,
        )
        assert runtime.model_runtime is not None
        completion_args["messages"] = history
        result = await runtime.model_runtime.stream_complete(**completion_args)
    return history, result


async def apply_pending_steers(
    session: AgentSession,
    request: AgentRequest,
    runtime: AgentRuntime,
    pending_tool_calls: list[ToolCallRequest] | tuple[ToolCallRequest, ...] = (),
) -> bool:
    """Apply queued user guidance only at an AgentLoop safe boundary."""
    steers = session.drain_steers()
    if not steers:
        return False

    skipped_message = "The user added steering guidance, so this tool call was not executed; re-plan with the new constraint."
    for call in pending_tool_calls:
        persist_message(session, tool_message(call.id, f"[not executed] {skipped_message}"))
        await session.events.put(
            tool_event(
                "tool.rejected",
                call.name,
                tool_call_id=call.id,
                error=skipped_message,
                data={"reason": "steer"},
            )
        )

    steer_text = "\n".join(f"- {message}" for message in steers)
    persist_message(
        session,
        user_note(
            f"The user added steering guidance during this run. Prioritize these latest constraints and adjust the remaining work:\n{steer_text}"
        ),
    )
    session.mark_agent_progress("run.steer.applied")
    if runtime.trace is not None:
        runtime.trace.record(
            "run.steer.applied",
            session_id=session.session_id,
            workspace=session.workspace,
            data={
                "run_id": session.current_run_id,
                "count": len(steers),
                "message_hashes": [stable_hash(message) for message in steers],
                "skipped_tool_calls": len(pending_tool_calls),
            },
        )
    await session.events.put(
        {
            "type": "run.steer.applied",
            "count": len(steers),
            "skipped_tool_calls": len(pending_tool_calls),
            "message": "Steering guidance was applied at an AgentLoop safe boundary.",
        }
    )
    return True


async def execute_gated(
    session: AgentSession,
    request: AgentRequest,
    call: ToolCallRequest,
    runtime: AgentRuntime,
    policy: PolicyEngine,
    context: Any,
) -> tuple[str, int]:
    parse_error = None
    if not isinstance(call.arguments, dict):
        parse_error_payload = tool_argument_parse_error(repr(call.arguments), ValueError("tool arguments JSON must be an object"))
        parse_error = parse_error_payload[TOOL_ARGUMENT_PARSE_ERROR_KEY]
    elif TOOL_ARGUMENT_PARSE_ERROR_KEY in call.arguments:
        parse_error = call.arguments[TOOL_ARGUMENT_PARSE_ERROR_KEY]
    if parse_error is not None:
        detail = parse_error.get("error", "") if isinstance(parse_error, dict) else str(parse_error)
        message = (
            f"Tool {call.name} received invalid JSON arguments and was not executed. "
            f"Regenerate valid JSON arguments before calling it again. {detail}"
        ).strip()
        if runtime.trace is not None:
            runtime.trace.record(
                "tool.argument_parse_error",
                session_id=session.session_id,
                workspace=session.workspace,
                data={"tool": call.name, "tool_call_id": call.id, "parse_error": parse_error},
            )
        await session.events.put(
            tool_event(
                "tool.error",
                call.name,
                tool_call_id=call.id,
                error=message,
                data={"parse_error": parse_error},
                duration_ms=0,
            )
        )
        return f"[tool argument parse failed] {message}", 0

    if runtime.tools is None:
        raise RuntimeError("AgentRuntime is missing a tool runtime adapter")
    validation_error = runtime.tools.validate_arguments(call.name, call.arguments)
    if validation_error is not None:
        message = (
            f"Tool {call.name} arguments failed validation and were not executed. "
            f"Regenerate arguments that match the tool schema. {validation_error}"
        ).strip()
        if runtime.trace is not None:
            runtime.trace.record(
                "tool.argument_validation_error",
                session_id=session.session_id,
                workspace=session.workspace,
                data={
                    "tool": call.name,
                    "tool_call_id": call.id,
                    "validation_error": validation_error,
                    "args": tool_audit_arguments(call.name, call.arguments),
                },
            )
        await session.events.put(
            tool_event(
                "tool.error",
                call.name,
                tool_call_id=call.id,
                error=message,
                data={"validation_error": validation_error},
                duration_ms=0,
            )
        )
        return f"[tool argument validation failed] {message}", 0

    spec = runtime.tools.spec_for(call.name)
    gate = policy.gate(
        call.name,
        call.arguments,
        mode=request.mode,
        # Read from the tool's own declaration; the policy engine no longer keeps
        # a second copy of which tools are read-only.
        read_only=bool(spec is not None and spec.read_only),
        workspace=context.workspace,
        protected_paths=context.protected_paths,
        trust_level=context.trust_level,
    )
    audit_args = tool_audit_arguments(call.name, call.arguments)
    if runtime.trace is not None:
        runtime.trace.record(
            "tool.started",
            session_id=session.session_id,
            workspace=session.workspace,
            data={
                "tool": call.name,
                "tool_call_id": call.id,
                "args": audit_args,
                "verdict": gate.verdict,
                "risk_level": gate.risk_level,
            },
        )
    await session.events.put(
        {
            "type": "tool.started",
            "tool": call.name,
            "tool_call_id": call.id,
            "args": call.arguments,
            "risk_level": gate.risk_level,
        }
    )

    if gate.verdict == "deny":
        await session.events.put(
            {
                "type": "tool.denied",
                "tool": call.name,
                "tool_call_id": call.id,
                "error": gate.reason,
                "risk_level": gate.risk_level,
            }
        )
        return f"[denied by policy] {gate.reason}", 0

    if spec is not None and spec.approval == "diff":
        # Declared behaviour, not a hardcoded tool name: any future tool that
        # needs diff approval routes here without editing the loop.
        return await execute_edit(session, request, call, runtime, context)

    if gate.verdict == "ask":
        decision = await request_approval(
            session,
            "tool",
            {"tool": call.name, "tool_call_id": call.id, "args": call.arguments, "reason": gate.reason},
            runtime,
        )
        if decision is not ApprovalDecision.ACCEPTED:
            reason = approval_failure_text(decision, "run this command")
            await session.events.put(
                {
                    "type": "tool.rejected",
                    "tool": call.name,
                    "tool_call_id": call.id,
                    "error": reason,
                    "risk_level": gate.risk_level,
                    "resolution": str(decision),
                }
            )
            return f"[{reason}]", 0

    session.mark_agent_progress(f"tool.{call.name}")
    context.tool_call_id = call.id
    result = await runtime.tools.run(call.name, call.arguments, context)
    if result.success:
        output = truncate_tool_output(call.name, result.text)
        if runtime.trace is not None:
            runtime.trace.record(
                "tool.finished",
                session_id=session.session_id,
                workspace=session.workspace,
                data=tool_finish_audit_data(call.name, result, tool_call_id=call.id),
            )
        await session.events.put(
            tool_event(
                "tool.output",
                call.name,
                tool_call_id=call.id,
                text=result.text[:2000],
                data=result.data,
                duration_ms=result.duration_ms,
            )
        )
        return output, 0
    if runtime.trace is not None:
        runtime.trace.record(
            "tool.finished",
            session_id=session.session_id,
            workspace=session.workspace,
            data=tool_finish_audit_data(call.name, result, tool_call_id=call.id),
        )
    await session.events.put(
        tool_event(
            "tool.error",
            call.name,
            tool_call_id=call.id,
            error=result.error,
            data=result.data,
            duration_ms=result.duration_ms,
        )
    )
    return f"[error] {truncate_tool_output(call.name, result.error)}", 0


async def execute_edit(
    session: AgentSession,
    request: AgentRequest,
    call: ToolCallRequest,
    runtime: AgentRuntime,
    context: Any,
) -> tuple[str, int]:
    if runtime.tools is None or runtime.clock is None:
        raise RuntimeError("AgentRuntime is missing a tool runtime or clock adapter")
    started = runtime.clock.monotonic()
    workspace = Path(request.workspace)
    try:
        proposal = runtime.tools.build_edit_proposal(workspace, call.arguments, context.protected_paths)
    except Exception as exc:
        await session.events.put(
            tool_event(
                "tool.error",
                "edit_file",
                tool_call_id=call.id,
                error=str(exc),
                duration_ms=elapsed_ms(started, runtime),
            )
        )
        return f"[edit failed] {exc}", 0

    auto = session.auto_accept_edits and not runtime.tools.is_protected_path(proposal.path, context.protected_paths)
    if auto:
        await session.events.put({"type": "edit.auto_approved", "path": proposal.path, "tool_call_id": call.id})
        decision = ApprovalDecision.ACCEPTED
    else:
        decision = await request_approval(
            session,
            "edit",
            {"path": proposal.path, "kind": proposal.kind, "diff": proposal.diff, "tool_call_id": call.id},
            runtime,
        )

    if decision is not ApprovalDecision.ACCEPTED:
        reason = approval_failure_text(decision, "apply this edit")
        await session.events.put(
            {
                "type": "edit.rejected",
                "path": proposal.path,
                "tool_call_id": call.id,
                "resolution": str(decision),
            }
        )
        return f"[{reason}] {proposal.path}", 0

    try:
        runtime.tools.apply_edit(workspace, proposal)
    except Exception as exc:
        await session.events.put(
            tool_event(
                "tool.error",
                "edit_file",
                tool_call_id=call.id,
                error=str(exc),
                duration_ms=elapsed_ms(started, runtime),
            )
        )
        marker = " stale" if "Stale" in exc.__class__.__name__ else ""
        return f"[edit failed{marker}] {exc}", 0
    duration_ms = elapsed_ms(started, runtime)
    patch_hash = stable_hash(proposal.diff)
    if runtime.trace is not None:
        runtime.trace.record(
            "edit.applied",
            session_id=session.session_id,
            workspace=session.workspace,
            data={
                "path": proposal.path,
                "kind": proposal.kind,
                "tool_call_id": call.id,
                "diff_bytes": len(proposal.diff),
                "patch_hash": patch_hash,
                "duration_ms": duration_ms,
            },
        )
    await session.events.put(
        {
            "type": "edit.applied",
            "path": proposal.path,
            "kind": proposal.kind,
            "tool_call_id": call.id,
            "patch_hash": patch_hash,
            "duration_ms": duration_ms,
        }
    )
    return f"Applied edit to {proposal.path}:\n{proposal.diff}", 1


def tool_event(
    event_type: str,
    tool: str,
    *,
    tool_call_id: str = "",
    text: str = "",
    error: str = "",
    data: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    payload = dict(data or {})
    event: dict[str, Any] = {"type": event_type, "tool": tool, "data": payload}
    if tool_call_id:
        event["tool_call_id"] = tool_call_id
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


def tool_finish_audit_data(tool: str, result: Any, *, tool_call_id: str = "") -> dict[str, Any]:
    data = {"tool": tool, "success": result.success, "duration_ms": result.duration_ms, "risk_level": result.risk_level}
    if tool_call_id:
        data["tool_call_id"] = tool_call_id
    data.update(tool_observability_fields(result.data))
    return data


def tool_audit_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool != "bash":
        return arguments
    command = str(arguments.get("command") or "")
    return {
        "command_hash": stable_hash(command),
        "timeout": arguments.get("timeout"),
    }


def elapsed_ms(started: float, runtime: AgentRuntime) -> int:
    assert runtime.clock is not None
    return max(0, int((runtime.clock.monotonic() - started) * 1000))


async def request_approval(
    session: AgentSession,
    kind: str,
    payload: dict[str, Any],
    runtime: AgentRuntime,
) -> ApprovalDecision:
    if runtime.approvals is None:
        raise RuntimeError("AgentRuntime is missing an approval broker")
    return await runtime.approvals.request(
        session,
        kind=kind,
        payload=payload,
    )


def approval_failure_text(decision: ApprovalDecision, action: str) -> str:
    """Explain to the model why an approval did not go through.

    A timeout deliberately reads differently from a refusal, and says not to
    treat it as one: nobody decided, so abandoning an otherwise correct plan
    would be the wrong response. It also tells the model to stop rather than
    silently retry, since a retry would just block on another unattended prompt.
    """
    if decision is ApprovalDecision.TIMED_OUT:
        return (
            f"approval to {action} timed out with no decision recorded; this is not a refusal. "
            "Stop and tell the user what needs approving instead of retrying"
        )
    if decision is ApprovalDecision.MISSING:
        return f"approval to {action} could not be found, so it was not performed"
    return f"user rejected the request to {action}"


def turn_budget(runtime: AgentRuntime) -> TurnBudget:
    """Resolve the per-turn budget from Runtime settings.

    Deliberately *not* overridable from `.aicode/config.json`: a spend limit that
    the inspected repository can raise is not a limit. This mirrors how Project
    Trust refuses to let a workspace grant itself trust.
    """
    settings = getattr(getattr(runtime.model_runtime, "settings", None), "budget", None)
    if settings is None:
        return TurnBudget()
    return TurnBudget(
        max_total_tokens=settings.max_total_tokens,
        max_total_cost=settings.max_total_cost,
    )


async def emit_budget_exceeded(
    session: AgentSession,
    runtime: AgentRuntime,
    budget: TurnBudget,
    ledger: TurnLedger,
    reason: str,
) -> None:
    payload = {
        "reason": reason,
        "total_tokens": ledger.total_tokens,
        "total_cost": round(ledger.total_cost, 8),
        "model_calls": ledger.model_calls,
        "limit": budget.max_steps if reason == "steps" else ledger.limit_for(budget, reason),
    }
    if runtime.trace is not None:
        runtime.trace.record(
            "run.budget.exceeded",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"run_id": session.current_run_id, **payload},
        )
    await session.events.put(
        {
            "type": "run.budget.exceeded",
            "message": f"The {reason} budget for this turn is exhausted; wrapping up without further tool calls.",
            **payload,
        }
    )


async def record_usage(
    session: AgentSession,
    result: CompletionResult,
    purpose: str,
    runtime: AgentRuntime,
) -> None:
    payload = {
        "model": result.model,
        "provider": result.provider,
        "purpose": purpose,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "estimated_cost": result.estimated_cost,
    }
    if runtime.trace is not None:
        runtime.trace.record(
            "usage.recorded",
            session_id=session.session_id,
            workspace=session.workspace,
            data=payload,
        )
    await session.events.put({"type": "usage.recorded", **payload})
