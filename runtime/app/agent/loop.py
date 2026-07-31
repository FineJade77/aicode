from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.agent.history import ContextManager, persist_message, truncate_tool_output
from app.agent.policy import PolicyEngine
from app.agent.progress import StallTracker
from app.agent.prompts import build_system_prompt, stall_note, verify_note, wind_down_note
from app.agent.session import AgentSession, ApprovalDecision
from app.agent.turn import TurnBudget, TurnLedger, assistant_message, tool_message, user_message, user_note
from app.agent.types import AgentRequest, AgentRuntime
from app.agent.verify import VerifyOutcome, VerifyTracker, summarize_failure
from app.models.provider import (
    TOOL_ARGUMENT_PARSE_ERROR_KEY,
    CompletionResult,
    ContextOverflowError,
    ToolCallRequest,
    tool_argument_parse_error,
)
from app.security import stable_hash
from app.tools.edit import file_hash


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
        session=session,
        approvals=runtime.approvals,
    )
    purpose = "reviewer" if request.mode == "review" else "main"
    model = str(getattr(request, "model", "") or "").strip() or None
    budget = turn_budget(runtime)
    ledger = TurnLedger()
    applied_edits = 0
    verify = VerifyTracker(limit=budget.max_verify_rounds)
    stall = StallTracker(limit=budget.max_repeated_actions)
    result: CompletionResult | None = None

    async def on_delta(text: str) -> None:
        session.mark_agent_progress("model.stream")
        await session.events.put({"type": "assistant.delta", "text": text})

    stop_reason: str | None = None
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

        stop_reason = ledger.exceeded(budget)
        if stop_reason is not None:
            await emit_budget_exceeded(session, runtime, budget, ledger, stop_reason)
            break

        if not result.tool_calls:
            if verify.exhausted(applied_edits):
                # Bounded, and it ends in the shared wind-down rather than in
                # silently exhausting the step budget.
                stop_reason = "verification"
                await emit_verification_exhausted(session, runtime, verify)
                break
            if verify.should_request_verification(applied_edits):
                round_number = verify.begin_round()
                session.mark_agent_progress(f"verify.round({round_number})")
                persist_message(
                    session,
                    user_note(verify_note(round_number, verify.limit, verify.failure_digest())),
                )
                continue
            session.mark_agent_progress("finalizing")
            break

        step = await execute_tool_calls(
            session,
            request,
            result.tool_calls,
            runtime,
            policy,
            context,
            edits_applied_before=applied_edits,
        )
        if step.applied_edits:
            verify.note_edit_applied()
        applied_edits += step.applied_edits
        for outcome in step.verifications:
            verify.record(outcome)
            await session.events.put(outcome.to_event(verify.rounds + 1, verify.limit))

        for tool_name, arguments, ok, output in step.executed:
            stall.observe(tool_name, arguments, ok=ok, output=output)
        if stall.should_stop():
            stop_reason = "no_progress"
            await emit_no_progress(session, runtime, stall, stopped=True)
            break
        warning = stall.pending_warning()
        if warning is not None:
            reason, count = warning
            session.mark_agent_progress(f"no_progress.{reason}")
            await emit_no_progress(session, runtime, stall, stopped=False, reason=reason, count=count)
            persist_message(session, user_note(stall_note(reason, count)))

    else:
        stop_reason = "steps"
        await emit_budget_exceeded(session, runtime, budget, ledger, stop_reason)

    if stop_reason is not None:
        # One shared wind-down for every early stop, so the user
        # always receives a summary rather than a truncated transcript. This call
        # is outside the loop and its usage is never re-gated, which is what stops
        # the wind-down from recursing into another budget stop.
        persist_message(session, user_note(wind_down_note(stop_reason)))
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


MAX_PARALLEL_TOOL_CALLS = 8


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """One tool call's outcome as the loop needs it.

    `ok` is carried explicitly rather than re-derived from `output`: every
    failure path formats its own `[...]` string, and pattern-matching those
    prefixes would silently misclassify the moment one of them is reworded.
    """

    output: str
    applied_edits: int = 0
    ok: bool = True
    # The tool's own exit code where it has one, not scraped back out of the
    # rendered text: failures are wrapped with an `[error] ` prefix, so any
    # parse of the output string is guessing at a format meant for the model.
    exit_code: int | None = None
    # Runtime-private provenance persisted with the tool message; stripped before
    # the history reaches a provider.
    meta: dict[str, Any] | None = None


@dataclass(slots=True)
class StepOutcome:
    """What one batch of tool calls produced, beyond their text results."""

    applied_edits: int = 0
    verifications: list[VerifyOutcome] = field(default_factory=list)
    # Every executed call in model order, so no-progress detection sees the same
    # sequence the model issued rather than a per-group view.
    executed: list[tuple[str, Any, bool, str]] = field(default_factory=list)


async def execute_tool_calls(
    session: AgentSession,
    request: AgentRequest,
    calls: list[ToolCallRequest],
    runtime: AgentRuntime,
    policy: PolicyEngine,
    context: Any,
    *,
    edits_applied_before: int = 0,
) -> StepOutcome:
    """Run one turn's tool calls, overlapping consecutive read-only ones.

    Models routinely return several `read_file`/`search` calls at once; running
    them one at a time makes the turn's latency the sum of them all.

    Only *consecutive* read-only calls are grouped, so relative order with
    writes is preserved: a read that the model placed after an edit still
    observes that edit. Read-only tools are also the only safe group to overlap
    for a second reason — their policy verdict is an immediate `allow`, so a
    parallel group can never sit on two approval prompts at once.

    Results are written back in the model's original call order regardless of
    completion order; tool results are paired to calls positionally by some
    providers, so completion order would corrupt the next request.
    """
    outcome = StepOutcome()
    edits_so_far = edits_applied_before
    for group in consecutive_tool_groups(calls, runtime):
        if len(group) > 1:
            session.mark_agent_progress(f"tool.parallel({len(group)})")
            semaphore = asyncio.Semaphore(MAX_PARALLEL_TOOL_CALLS)
            outcomes = list(
                await asyncio.gather(
                    *(
                        _execute_with_limit(semaphore, session, request, call, runtime, policy, context)
                        for call in group
                    )
                )
            )
        else:
            session.mark_agent_progress(f"tool.{group[0].name}")
            outcomes = [await execute_gated(session, request, group[0], runtime, policy, context)]

        for call, call_result in zip(group, outcomes, strict=True):
            outcome.applied_edits += call_result.applied_edits
            if call.name == "bash" and edits_so_far > 0:
                # Any shell run *after* an edit counts as an attempt to verify.
                # Which command "really" verifies is not something the Runtime can
                # decide for an arbitrary project, so the bound is on repair
                # rounds rather than on command identity. Checked against the
                # running count, not the batch's starting count, so an edit and
                # its test in one assistant message are still ordered correctly.
                outcome.verifications.append(verification_from_result(call, call_result))
            edits_so_far += call_result.applied_edits
            outcome.executed.append((call.name, call.arguments, call_result.ok, call_result.output))
            persist_message(session, tool_message(call.id, call_result.output, call_result.meta))
    return outcome


async def _execute_with_limit(
    semaphore: asyncio.Semaphore,
    session: AgentSession,
    request: AgentRequest,
    call: ToolCallRequest,
    runtime: AgentRuntime,
    policy: PolicyEngine,
    context: Any,
) -> ToolCallResult:
    """Bound how many tool calls run at once.

    A model can return dozens of reads in one turn; without a cap they would all
    open files and subprocesses simultaneously.
    """
    async with semaphore:
        return await execute_gated(session, request, call, runtime, policy, context)


def tool_call_meta(tool_name: str, result: Any) -> dict[str, Any] | None:
    """Provenance the compaction path needs but the model must never see.

    Recorded per read rather than derived later from `session.read_files`: that
    record is updated on write too, so after the agent edits a file it already
    matches disk and could no longer show that an earlier read went stale.
    """
    if tool_name != "read_file" or not isinstance(getattr(result, "data", None), dict):
        return None
    path = result.data.get("read_path")
    content_hash = result.data.get("content_hash")
    if not path or not content_hash:
        return None
    return {"read": {"path": str(path), "hash": str(content_hash)}}


def tool_exit_code(result: Any) -> int | None:
    value = result.data.get("exit_code") if isinstance(getattr(result, "data", None), dict) else None
    return value if isinstance(value, int) else None


def verification_from_result(call: ToolCallRequest, result: ToolCallResult) -> VerifyOutcome:
    """Read a bash call's outcome as a verification attempt.

    A command that was denied, rejected, or never parsed has no exit code at all;
    it reports as failed with the placeholder, because it did not run and so
    verified nothing.
    """
    return VerifyOutcome(
        command=str(call.arguments.get("command") or ""),
        passed=result.ok and result.exit_code == 0,
        exit_code=result.exit_code if result.exit_code is not None else -1,
        summary="" if result.ok else summarize_failure(result.output),
    )


def consecutive_tool_groups(
    calls: list[ToolCallRequest],
    runtime: AgentRuntime,
) -> list[list[ToolCallRequest]]:
    """Split calls into runs that may overlap, preserving the model's order.

    A run of read-only calls becomes one group; every other call is its own
    group. Grouping only consecutive calls is what keeps read-after-write
    ordering intact.
    """
    groups: list[list[ToolCallRequest]] = []
    for call in calls:
        spec = runtime.tools.spec_for(call.name) if runtime.tools is not None else None
        parallelizable = bool(spec is not None and spec.read_only)
        if parallelizable and groups and _group_is_parallel(groups[-1], runtime):
            groups[-1].append(call)
            continue
        groups.append([call])
    return groups


def _group_is_parallel(group: list[ToolCallRequest], runtime: AgentRuntime) -> bool:
    spec = runtime.tools.spec_for(group[0].name) if runtime.tools is not None else None
    return bool(spec is not None and spec.read_only)


async def execute_gated(
    session: AgentSession,
    request: AgentRequest,
    call: ToolCallRequest,
    runtime: AgentRuntime,
    policy: PolicyEngine,
    context: Any,
) -> ToolCallResult:
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
        return ToolCallResult(f"[tool argument parse failed] {message}", ok=False)

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
        return ToolCallResult(f"[tool argument validation failed] {message}", ok=False)

    spec = runtime.tools.spec_for(call.name)
    gate = policy.gate(
        call.name,
        call.arguments,
        mode=request.mode,
        # The tool's own declaration drives the verdict; the policy engine keeps
        # no second copy of which tools are read-only or need approval.
        spec=spec,
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
        return ToolCallResult(f"[denied by policy] {gate.reason}", ok=False)

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
            return ToolCallResult(f"[{reason}]", ok=False)

    session.mark_agent_progress(f"tool.{call.name}")
    # A per-call copy rather than assigning onto the shared context: overlapping
    # calls would otherwise race on tool_call_id and mislabel each other's audit
    # and execution records.
    call_context = replace(context, tool_call_id=call.id)
    result = await runtime.tools.run(call.name, call.arguments, call_context)
    if call.name == "update_plan" and result.success:
        # Surfaced as its own event so the CLI can render a progress list rather
        # than a wall of tool output.
        await session.events.put({"type": "plan.updated", "items": result.data.get("items") or []})
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
        return ToolCallResult(output, exit_code=tool_exit_code(result), meta=tool_call_meta(call.name, result))
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
    return ToolCallResult(
        f"[error] {truncate_tool_output(call.name, result.error)}",
        ok=False,
        exit_code=tool_exit_code(result),
    )


async def execute_edit(
    session: AgentSession,
    request: AgentRequest,
    call: ToolCallRequest,
    runtime: AgentRuntime,
    context: Any,
) -> ToolCallResult:
    if runtime.tools is None or runtime.clock is None:
        raise RuntimeError("AgentRuntime is missing a tool runtime or clock adapter")
    started = runtime.clock.monotonic()
    workspace = Path(request.workspace)
    try:
        proposal = runtime.tools.build_edit_proposal(context, call.arguments)
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
        return ToolCallResult(f"[edit failed] {exc}", ok=False)

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
        return ToolCallResult(f"[{reason}] {proposal.path}", ok=False)

    try:
        runtime.tools.apply_edit(workspace, proposal)
        # The model has just seen this content, so keep the read record current;
        # otherwise a second edit to the same file would demand a pointless re-read.
        record_written_file(session, workspace, proposal)
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
        return ToolCallResult(f"[edit failed{marker}] {exc}", ok=False)
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
    return ToolCallResult(f"Applied edit to {proposal.path}:\n{proposal.diff}", applied_edits=1)


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


def record_written_file(session: AgentSession, workspace: Path, proposal: Any) -> None:
    target = workspace / proposal.path
    if proposal.kind == "delete" or not target.is_file():
        session.forget_read(proposal.path)
        return
    session.record_read(proposal.path, file_hash(target))


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
        max_verify_rounds=settings.max_verify_rounds,
        max_repeated_actions=settings.max_repeated_actions,
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


async def emit_no_progress(
    session: AgentSession,
    runtime: AgentRuntime,
    stall: StallTracker,
    *,
    stopped: bool,
    reason: str | None = None,
    count: int = 0,
) -> None:
    """Report repetition, both the warning and the stop.

    One event type for both so a consumer sees the whole escalation on a single
    stream rather than having to correlate two.
    """
    payload = {
        "reason": reason or stall.tripped_reason or "",
        "count": count or stall.tripped_count,
        "limit": stall.limit,
        "stopped": stopped,
    }
    if runtime.trace is not None:
        runtime.trace.record(
            "run.no_progress",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"run_id": session.current_run_id, **payload},
        )
    message = (
        f"No progress: the same action repeated {payload['count']} times; wrapping up with a report."
        if stopped
        else f"No progress: the same action repeated {payload['count']} times; asking the agent to change approach."
    )
    await session.events.put({"type": "run.no_progress", "message": message, **payload})


async def emit_verification_exhausted(
    session: AgentSession,
    runtime: AgentRuntime,
    verify: VerifyTracker,
) -> None:
    """Report that the repair loop hit its bound with verification still failing.

    Carries the extracted failure summary rather than the raw command output: the
    point of the bound is that the user learns *why* it is still failing without
    reading the whole transcript.
    """
    latest = verify.failures[-1] if verify.failures else None
    payload = {
        "rounds": verify.rounds,
        "limit": verify.limit,
        "command": latest.command if latest is not None else "",
        "exit_code": latest.exit_code if latest is not None else 0,
        "summary": latest.summary if latest is not None else "",
    }
    if runtime.trace is not None:
        runtime.trace.record(
            "run.verification.exhausted",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"run_id": session.current_run_id, **payload},
        )
    await session.events.put(
        {
            "type": "run.verification.exhausted",
            "message": (
                f"Verification did not pass in {verify.rounds} attempts; wrapping up with a report "
                "instead of continuing to edit."
            ),
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
