"""Read-only exploration in its own context.

Started by explicit user decision, not by the evidence gate: across 42 live runs
exploration was 57% of tool output but produced zero compactions and zero
context-attributed failures, so the roadmap's trigger was not met. That is
recorded in TASKS.md; these tests pin the boundaries that make the feature safe
regardless of whether it proves valuable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agent.subagent import (
    DELEGATE_TOOLS,
    MAX_REPORT_CHARS,
    SUBAGENT_TOOLS,
    run_subagent,
    subagent_budget,
)
from app.agent.turn import TurnBudget, TurnLedger
from app.agent.types import AgentRuntime
from app.models.provider import CompletionResult, ToolCallRequest
from app.tools.registry import build_tool_context
from app.tools.runtime import DefaultToolRuntime


class ScriptedRouter:
    """Returns queued completions; records the tool schemas it was offered."""

    settings = None
    primary = None

    def __init__(self, *results: CompletionResult) -> None:
        self.results = list(results)
        self.offered: list[list[str]] = []

    async def stream_complete(self, **kwargs):
        self.offered.append([schema["name"] for schema in kwargs.get("tools") or []])
        return self.results.pop(0) if self.results else text_result("done")


def text_result(text: str, *, tokens: int = 100, cost: float = 0.001) -> CompletionResult:
    return CompletionResult(
        text=text, model="m", provider="p", input_tokens=tokens, output_tokens=10,
        estimated_cost=cost, tool_calls=[],
    )


def call_result(name: str, arguments: dict, *, call_id: str = "c1") -> CompletionResult:
    return CompletionResult(
        text="", model="m", provider="p", input_tokens=100, output_tokens=10, estimated_cost=0.001,
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=arguments)],
    )


def make_runtime(router: ScriptedRouter) -> AgentRuntime:
    return AgentRuntime(model_runtime=router, trace=None, tools=DefaultToolRuntime())


def small_budget(**overrides) -> TurnBudget:
    base = {"max_total_tokens": 100_000, "max_total_cost": 1.0, "max_steps": 6}
    base.update(overrides)
    return TurnBudget(**base)


def test_the_budget_is_carved_from_what_the_parent_has_left() -> None:
    """A fresh budget per child is how a turn escapes its own spend limit."""
    parent = TurnBudget(max_total_tokens=1_000_000, max_total_cost=4.0)
    spent = TurnLedger()
    spent.total_tokens = 800_000
    spent.total_cost = 3.0

    child = subagent_budget(parent, spent)

    assert child.max_total_tokens == 50_000  # (1,000,000 - 800,000) * 0.25
    assert child.max_total_cost == pytest.approx(0.25)
    assert child.max_steps <= parent.max_steps


def test_a_spent_parent_leaves_nothing_to_delegate() -> None:
    parent = TurnBudget(max_total_tokens=1_000, max_total_cost=1.0)
    spent = TurnLedger()
    spent.total_tokens = 1_000

    assert subagent_budget(parent, spent).max_total_tokens == 0


@pytest.mark.asyncio
async def test_only_read_only_tools_are_offered(tmp_path: Path) -> None:
    router = ScriptedRouter(text_result("answer"))

    await run_subagent(
        runtime=make_runtime(router),
        context=build_tool_context(str(tmp_path), "default"),
        question="where?",
        budget=small_budget(),
    )

    assert set(router.offered[0]) == set(SUBAGENT_TOOLS)
    for forbidden in ("edit_file", "bash", "explore", "ask_user", "update_plan"):
        assert forbidden not in router.offered[0]


@pytest.mark.asyncio
async def test_a_tool_it_was_not_offered_is_refused(tmp_path: Path) -> None:
    """Filtering the schemas is only half of an allowlist.

    A model can emit a name it was never shown, and honouring it would let a
    subagent reach a tool the caller did not grant — including `edit_file`,
    whose approval prompt nobody would see.
    """
    router = ScriptedRouter(
        call_result("edit_file", {"path": "a.py", "old_text": "x", "new_text": "y"}),
        text_result("done"),
    )
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")

    await run_subagent(
        runtime=make_runtime(router),
        context=build_tool_context(str(tmp_path), "default"),
        question="edit it",
        budget=small_budget(),
    )

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x\n"


@pytest.mark.asyncio
async def test_a_subagent_cannot_spawn_a_subagent(tmp_path: Path) -> None:
    """Depth one. Budget accounting over a tree is a problem worth not having."""
    router = ScriptedRouter(call_result("explore", {"question": "deeper"}), text_result("done"))

    outcome = await run_subagent(
        runtime=make_runtime(router),
        context=build_tool_context(str(tmp_path), "default"),
        question="go deeper",
        budget=small_budget(),
    )

    assert "explore" not in router.offered[0]
    assert outcome.model_calls == 2


@pytest.mark.asyncio
async def test_the_report_replaces_the_reading(tmp_path: Path) -> None:
    """The whole point: the caller gains a paragraph, not the files."""
    body = "\n".join(f"# line {index}" for index in range(400))
    (tmp_path / "big.py").write_text(body, encoding="utf-8")
    router = ScriptedRouter(
        call_result("read_file", {"path": "big.py"}),
        text_result("It is in big.py:1-400."),
    )

    outcome = await run_subagent(
        runtime=make_runtime(router),
        context=build_tool_context(str(tmp_path), "default"),
        question="where?",
        budget=small_budget(),
    )

    assert outcome.report == "It is in big.py:1-400."
    assert len(outcome.report) < len(body) / 10
    assert outcome.tool_calls == 1


@pytest.mark.asyncio
async def test_an_exhausted_budget_stops_the_subagent(tmp_path: Path) -> None:
    """Tool calls, not text: a text-only reply ends the subagent by answering,
    which is a different exit than being cut off."""
    router = ScriptedRouter(
        *[
            CompletionResult(
                text="", model="m", provider="p", input_tokens=5_000, output_tokens=10,
                estimated_cost=0.001,
                tool_calls=[ToolCallRequest(id=f"c{index}", name="list_files", arguments={"path": "."})],
            )
            for index in range(6)
        ]
    )

    outcome = await run_subagent(
        runtime=make_runtime(router),
        context=build_tool_context(str(tmp_path), "default"),
        question="loop",
        budget=small_budget(max_total_tokens=6_000),
    )

    assert outcome.stop_reason == "tokens"
    assert outcome.total_tokens >= 5_000


@pytest.mark.asyncio
async def test_a_runaway_subagent_stops_at_the_step_cap(tmp_path: Path) -> None:
    router = ScriptedRouter(*[call_result("list_files", {"path": "."}, call_id=f"c{i}") for i in range(20)])

    outcome = await run_subagent(
        runtime=make_runtime(router),
        context=build_tool_context(str(tmp_path), "default"),
        question="loop",
        budget=small_budget(max_steps=3),
    )

    assert outcome.stop_reason == "steps"
    assert outcome.model_calls == 3


@pytest.mark.asyncio
async def test_a_failing_tool_does_not_end_the_subagent(tmp_path: Path) -> None:
    """A missing file is a fact to report, not a crash to propagate upward."""
    router = ScriptedRouter(
        call_result("read_file", {"path": "nope.py"}),
        text_result("That file does not exist."),
    )

    outcome = await run_subagent(
        runtime=make_runtime(router),
        context=build_tool_context(str(tmp_path), "default"),
        question="read it",
        budget=small_budget(),
    )

    assert outcome.report == "That file does not exist."


@pytest.mark.asyncio
async def test_an_oversized_report_is_truncated(tmp_path: Path) -> None:
    """A report longer than the reading it replaces defeats the purpose."""
    router = ScriptedRouter(text_result("x" * (MAX_REPORT_CHARS + 1_000)))

    outcome = await run_subagent(
        runtime=make_runtime(router),
        context=build_tool_context(str(tmp_path), "default"),
        question="ramble",
        budget=small_budget(),
    )

    assert "report truncated" in outcome.report
    assert len(outcome.report) <= MAX_REPORT_CHARS + 50


@pytest.mark.asyncio
async def test_a_silent_subagent_still_reports_something(tmp_path: Path) -> None:
    router = ScriptedRouter(text_result(""))

    outcome = await run_subagent(
        runtime=make_runtime(router),
        context=build_tool_context(str(tmp_path), "default"),
        question="say nothing",
        budget=small_budget(),
    )

    assert outcome.report.strip()


# --- reviewing your own change ------------------------------------------------


@pytest.mark.asyncio
async def test_a_subagent_can_reach_the_deterministic_reviewer(tmp_path: Path) -> None:
    """`review_diff` is what makes a review subagent more than a second opinion.

    Without it the reviewer would be re-reading files and guessing what changed;
    with it, it starts from the 18 deterministic findings and spends its budget
    on what those rules cannot see.
    """
    router = ScriptedRouter(text_result("no problems found"))

    await run_subagent(
        runtime=make_runtime(router),
        context=build_tool_context(str(tmp_path), "default"),
        question="review my changes",
        budget=small_budget(),
    )

    assert "review_diff" in router.offered[0]


@pytest.mark.asyncio
async def test_the_reviewer_still_cannot_write(tmp_path: Path) -> None:
    """Adding the reviewer must not widen the surface.

    A subagent that can fix what it found would be applying edits the user never
    saw proposed.
    """
    router = ScriptedRouter(text_result("ok"))

    await run_subagent(
        runtime=make_runtime(router),
        context=build_tool_context(str(tmp_path), "default"),
        question="review my changes",
        budget=small_budget(),
    )

    for forbidden in ("edit_file", "bash", "explore"):
        assert forbidden not in router.offered[0]


def test_the_prompt_asks_for_a_review_only_when_it_is_worth_it(tmp_path: Path) -> None:
    """A rule that fires on every one-line fix is a tax, not a check.

    The wording has to carry the exemption, because the model has no other way
    to know the review costs a quarter of the remaining budget.
    """
    from app.agent.prompts import build_system_prompt
    from app.tools.workspace import LocalWorkspaceRuntime

    class Request:
        workspace = str(tmp_path)
        mode = "default"
        message = "go"

    prompt = build_system_prompt(Request(), LocalWorkspaceRuntime().prompt_context(tmp_path))

    assert "review my changes" in prompt
    assert "One-file fixes do not need it" in prompt


# --- delegated work -----------------------------------------------------------
#
# The read-only variant answers questions; this one does the job. What makes
# that safe is not a wider allowlist but a shared gate: every call a worker makes
# goes through the same `execute_gated` the parent uses, so the policy verdict,
# the approval prompt, the events and the audit record are identical.


def working_runtime(router: ScriptedRouter, session, approvals):
    from app.agent.policy import PolicyEngine
    from app.system import SystemClock

    return AgentRuntime(
        model_runtime=router, trace=None, tools=DefaultToolRuntime(),
        clock=SystemClock(), approvals=approvals, policy=PolicyEngine(),
    )


class Request:
    def __init__(self, workspace: Path) -> None:
        self.workspace = str(workspace)
        self.mode = "default"
        self.message = "go"


async def approve_everything(session) -> None:
    while True:
        await asyncio.sleep(0.005)
        for approval in list(session.approvals.values()):
            if approval.accepted is None:
                session.resolve_approval(approval.approval_id, accepted=True)


@pytest.mark.asyncio
async def test_a_delegated_edit_raises_the_users_approval(tmp_path: Path) -> None:
    """The reason a worker may write at all.

    An edit made inside a child that never reaches the approval broker would be
    a write the user never agreed to, made by a run they cannot see.
    """
    from app.agent.loop import execute_subagent
    from app.sessions.approvals import SessionApprovalBroker
    from app.sessions.store import SessionStore
    from app.tools.edit import file_hash

    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    session = SessionStore(tmp_path / "s.sqlite").create(workspace=str(tmp_path))
    session.record_read("calc.py", file_hash(tmp_path / "calc.py"))
    router = ScriptedRouter(
        call_result("edit_file", {"path": "calc.py", "old_text": "return a - b", "new_text": "return a + b"}),
        text_result("Fixed calc.py"),
    )
    approvals = SessionApprovalBroker(timeout_seconds=5)
    runtime = working_runtime(router, session, approvals)
    context = build_tool_context(str(tmp_path), "default", session=session, approvals=approvals)

    approver = asyncio.create_task(approve_everything(session))
    outcome = await execute_subagent(
        session, Request(tmp_path),
        ToolCallRequest(id="d1", name="delegate", arguments={"task": "fix add"}),
        runtime, runtime.policy, context, TurnLedger(), small_budget(),
    )
    approver.cancel()

    events = [event["type"] for event in session.events.events_after(0)]
    assert "approval.requested" in events
    assert "edit.applied" in events
    assert (tmp_path / "calc.py").read_text(encoding="utf-8").strip().endswith("return a + b")
    # Counted on the parent, so verification and wind-down see the real work.
    assert outcome.applied_edits == 1


@pytest.mark.asyncio
async def test_a_delegated_edit_that_is_refused_is_not_applied(tmp_path: Path) -> None:
    from app.agent.loop import execute_subagent
    from app.sessions.approvals import SessionApprovalBroker
    from app.sessions.store import SessionStore
    from app.tools.edit import file_hash

    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    session = SessionStore(tmp_path / "s.sqlite").create(workspace=str(tmp_path))
    session.record_read("calc.py", file_hash(tmp_path / "calc.py"))
    router = ScriptedRouter(
        call_result("edit_file", {"path": "calc.py", "old_text": "return a - b", "new_text": "return a + b"}),
        text_result("could not apply"),
    )
    approvals = SessionApprovalBroker(timeout_seconds=5)
    runtime = working_runtime(router, session, approvals)
    context = build_tool_context(str(tmp_path), "default", session=session, approvals=approvals)

    async def refuse() -> None:
        while True:
            await asyncio.sleep(0.005)
            for approval in list(session.approvals.values()):
                if approval.accepted is None:
                    session.resolve_approval(approval.approval_id, accepted=False)

    refuser = asyncio.create_task(refuse())
    outcome = await execute_subagent(
        session, Request(tmp_path),
        ToolCallRequest(id="d1", name="delegate", arguments={"task": "fix add"}),
        runtime, runtime.policy, context, TurnLedger(), small_budget(),
    )
    refuser.cancel()

    assert "return a - b" in (tmp_path / "calc.py").read_text(encoding="utf-8")
    assert outcome.applied_edits == 0


@pytest.mark.asyncio
async def test_explore_still_cannot_reach_a_write_tool(tmp_path: Path) -> None:
    """Adding a working variant must not widen the read-only one."""
    from app.agent.loop import execute_subagent
    from app.sessions.store import SessionStore

    session = SessionStore(tmp_path / "s.sqlite").create(workspace=str(tmp_path))
    router = ScriptedRouter(text_result("answer"))
    runtime = AgentRuntime(model_runtime=router, trace=None, tools=DefaultToolRuntime())
    context = build_tool_context(str(tmp_path), "default", session=session)

    await execute_subagent(
        session, Request(tmp_path),
        ToolCallRequest(id="e1", name="explore", arguments={"question": "where?"}),
        runtime, None, context, TurnLedger(), small_budget(),
    )

    for forbidden in ("edit_file", "bash", "delegate", "explore"):
        assert forbidden not in router.offered[0]


@pytest.mark.asyncio
async def test_a_worker_cannot_delegate_further(tmp_path: Path) -> None:
    from app.sessions.store import SessionStore

    session = SessionStore(tmp_path / "s.sqlite").create(workspace=str(tmp_path))
    router = ScriptedRouter(text_result("done"))
    runtime = AgentRuntime(model_runtime=router, trace=None, tools=DefaultToolRuntime())

    await run_subagent(
        runtime=runtime,
        context=build_tool_context(str(tmp_path), "default", session=session),
        question="do the thing",
        budget=small_budget(),
        allowed=DELEGATE_TOOLS,
    )

    assert "delegate" not in router.offered[0]
    assert "explore" not in router.offered[0]
    assert "edit_file" in router.offered[0]
