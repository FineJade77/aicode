import asyncio
import json
import time

import pytest

from app.adapters.approvals import SessionApprovalBroker
from app.adapters.system import SystemClock
from app.adapters.tools import DefaultToolRuntime
from app.adapters.workspace import LocalWorkspaceRuntime
from app.agent.loop import complete_with_compaction, consecutive_tool_groups, run_turn
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger, stable_hash
from app.config.settings import Settings
from app.models.provider import (
    TOOL_ARGUMENT_PARSE_ERROR_KEY,
    ContextOverflowError,
    StreamEvent,
    ToolCallRequest,
    Usage,
)
from app.models.router import ModelRouter
from app.policy.engine import PolicyEngine
from app.project.trust import TrustStore
from app.sessions.store import SessionStore
from app.usage.pricing import ModelPrice
from tests.fakes import FakeProvider, text_turn, tool_turn


class Request:
    def __init__(self, workspace, message="Fix a bug", mode="default", model=None):
        self.workspace = str(workspace)
        self.message = message
        self.mode = mode
        self.model = model


def make_runtime(turns, tmp_path):
    fake = FakeProvider(turns)
    router = ModelRouter(primary=fake, settings=Settings())
    audit = AuditLogger(path=tmp_path / "audit.jsonl")
    return (
        AgentRuntime(
            model_runtime=router,
            trace=audit,
            policy=PolicyEngine(),
            tools=DefaultToolRuntime(),
            workspace=LocalWorkspaceRuntime(),
            clock=SystemClock(),
            approvals=SessionApprovalBroker(),
        ),
        fake,
    )


def make_session(tmp_path):
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path))
    return session


def events_of(session, event_type):
    return [e for e in session.events.events_after(0) if e.get("type") == event_type]


class OverflowThenTextProvider:
    provider_name = "fake"

    def __init__(self, failures: int):
        self.failures = failures
        self.main_calls = 0
        self.calls = []

    def is_configured(self):
        return True

    async def stream_complete(self, request):
        self.calls.append(request)
        if request.purpose == "summarizer":
            yield StreamEvent(type="text_delta", text="- Preserve the current goal and verification results")
            yield StreamEvent(type="done", usage=Usage(20, 8), model="summary-model")
            return
        self.main_calls += 1
        if self.main_calls <= self.failures:
            raise ContextOverflowError("maximum context length exceeded")
        yield StreamEvent(type="text_delta", text="Recovery succeeded")
        yield StreamEvent(type="done", usage=Usage(20, 8), model="main-model")


@pytest.mark.asyncio
async def test_plain_text_turn_emits_final(tmp_path):
    runtime, _ = make_runtime([text_turn("Nothing to change")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    finals = events_of(session, "final")
    assert finals and "Nothing to change" in finals[0]["summary"]
    assert events_of(session, "assistant.delta")


@pytest.mark.asyncio
async def test_message_can_override_model_without_changing_route(tmp_path):
    runtime, fake = make_runtime([text_turn("Used the model override")], tmp_path)
    session = make_session(tmp_path)

    await run_turn(session, Request(tmp_path, model="local-coder"), runtime)

    assert fake.calls[0].model == "local-coder"


@pytest.mark.asyncio
async def test_steer_skips_pending_tool_call_at_safe_boundary(tmp_path):
    session = make_session(tmp_path)

    class SteeringProvider(FakeProvider):
        async def stream_complete(self, request):
            self.calls.append(request)
            turn = self.turns.pop(0)
            for event in turn:
                if event.type == "tool_call":
                    session.enqueue_steer("Do not execute tools; explain the risk first")
                yield event

    fake = SteeringProvider(
        [tool_turn("bash", {"command": "touch should-not-exist"}), text_turn("Adjusted to the new constraint")]
    )
    runtime = AgentRuntime(
        model_runtime=ModelRouter(primary=fake, settings=Settings()),
        trace=AuditLogger(path=tmp_path / "audit.jsonl"),
        policy=PolicyEngine(),
        tools=DefaultToolRuntime(),
        workspace=LocalWorkspaceRuntime(),
        clock=SystemClock(),
        approvals=SessionApprovalBroker(),
    )

    await run_turn(session, Request(tmp_path), runtime)

    assert not events_of(session, "tool.started")
    rejected = events_of(session, "tool.rejected")
    assert rejected and rejected[0]["data"]["reason"] == "steer"
    assert events_of(session, "run.steer.applied")[0]["skipped_tool_calls"] == 1
    assert any(
        "Do not execute tools" in str(message.get("content"))
        for message in fake.calls[1].messages
        if message.get("role") == "user"
    )


@pytest.mark.asyncio
async def test_context_overflow_forces_one_compaction_retry(tmp_path):
    provider = OverflowThenTextProvider(failures=1)
    router = ModelRouter(primary=provider, settings=Settings())
    runtime = AgentRuntime(
        model_runtime=router,
        trace=AuditLogger(path=tmp_path / "audit.jsonl"),
        policy=PolicyEngine(),
    )
    store = SessionStore(path=tmp_path / "overflow.sqlite")
    session = store.create(workspace=str(tmp_path))
    for index in range(4):
        store.append_message(session, {"role": "user", "content": f"Historical goal {index}"})

    async def on_delta(_text):
        return None

    history, result = await complete_with_compaction(
        session=session,
        runtime=runtime,
        purpose="main",
        system="system",
        tools=[],
        on_text_delta=on_delta,
        max_tokens=1_024,
    )

    assert result.text == "Recovery succeeded"
    assert provider.main_calls == 2
    assert len(session.compactions) == 1
    assert history[0]["content"].startswith("[persistent history summary")
    budget_event = events_of(session, "context.budget")[-1]
    assert budget_event["forced"] is True
    assert budget_event["reason"] == "provider_overflow"


@pytest.mark.asyncio
async def test_context_overflow_is_never_retried_more_than_once(tmp_path):
    provider = OverflowThenTextProvider(failures=99)
    router = ModelRouter(primary=provider, settings=Settings())
    runtime = AgentRuntime(
        model_runtime=router,
        trace=AuditLogger(path=tmp_path / "audit.jsonl"),
        policy=PolicyEngine(),
    )
    store = SessionStore(path=tmp_path / "overflow-twice.sqlite")
    session = store.create(workspace=str(tmp_path))
    for index in range(4):
        store.append_message(session, {"role": "user", "content": f"Historical goal {index}"})

    async def on_delta(_text):
        return None

    with pytest.raises(ContextOverflowError):
        await complete_with_compaction(
            session=session,
            runtime=runtime,
            purpose="main",
            system="system",
            tools=[],
            on_text_delta=on_delta,
            max_tokens=1_024,
        )

    assert provider.main_calls == 2


@pytest.mark.asyncio
async def test_tool_loop_executes_and_feeds_back(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, fake = make_runtime(
        [tool_turn("read_file", {"path": "a.py"}), text_turn("Read complete")], tmp_path
    )
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    output_events = events_of(session, "tool.output")
    assert output_events
    assert output_events[0]["tool_call_id"] == "tc_1"
    assert isinstance(output_events[0]["duration_ms"], int)
    started_events = events_of(session, "tool.started")
    assert started_events and started_events[0]["tool_call_id"] == "tc_1"
    # The second model call includes the tool result.
    second_call = fake.calls[1]
    assert any(m.get("role") == "tool" and "x = 1" in str(m.get("content")) for m in second_call.messages)


@pytest.mark.asyncio
async def test_tool_output_exposes_command_observability_fields(tmp_path):
    runtime, _ = make_runtime([tool_turn("bash", {"command": "ls"}), text_turn("Listing complete")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    output = events_of(session, "tool.output")[0]
    assert output["tool_call_id"] == "tc_1"
    assert output["exit_code"] == 0
    assert output["data"]["exit_code"] == 0
    assert isinstance(output["duration_ms"], int)
    await runtime.trace.flush()
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    finished = [record for record in records if record["event_type"] == "tool.finished"]
    assert finished and finished[0]["data"]["tool_call_id"] == "tc_1"


@pytest.mark.asyncio
async def test_untrusted_project_command_requires_approval_in_agent_loop(tmp_path):
    runtime, fake = make_runtime(
        [tool_turn("bash", {"command": "pytest --version"}), text_turn("Not executed")],
        tmp_path,
    )
    runtime.trust = TrustStore(tmp_path.parent / f"{tmp_path.name}-state" / "trust.json")
    session = make_session(tmp_path)

    async def reject_soon():
        for _ in range(100):
            await asyncio.sleep(0.01)
            pending = [approval for approval in session.approvals.values() if approval.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=False)
                return

    _task = asyncio.create_task(reject_soon())
    await run_turn(session, Request(tmp_path), runtime)

    approvals = events_of(session, "approval.requested")
    assert approvals
    assert approvals[0]["tool"] == "bash"
    assert "untrusted" in approvals[0]["reason"]
    assert any("rejected" in str(message.get("content")) for message in fake.calls[1].messages if message.get("role") == "tool")


@pytest.mark.asyncio
async def test_malformed_tool_arguments_feed_parse_error_to_model(tmp_path):
    runtime, fake = make_runtime(
        [
            tool_turn(
                "read_file",
                {TOOL_ARGUMENT_PARSE_ERROR_KEY: {"error": "Expecting value", "raw_arguments": "{bad", "truncated": False}},
            ),
            text_turn("I will retry with valid JSON"),
        ],
        tmp_path,
    )
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    errors = events_of(session, "tool.error")
    assert errors and errors[0]["data"]["parse_error"]["raw_arguments"] == "{bad"
    assert errors[0]["tool_call_id"] == "tc_1"
    assert errors[0]["parse_error"]["raw_arguments"] == "{bad"
    assert errors[0]["duration_ms"] == 0
    assert not events_of(session, "tool.started")
    second_call = fake.calls[1]
    assert any("tool argument parse failed" in str(m.get("content")) for m in second_call.messages if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_non_dict_tool_arguments_do_not_reach_policy(tmp_path):
    runtime, fake = make_runtime([tool_turn("read_file", [], call_id="tc_bad"), text_turn("I will retry")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    assert not events_of(session, "tool.started")
    error = events_of(session, "tool.error")[0]
    assert error["data"]["parse_error"]["raw_arguments"] == "[]"
    assert error["tool_call_id"] == "tc_bad"
    assert error["parse_error"]["raw_arguments"] == "[]"
    assert any("tool arguments JSON must be an object" in str(m.get("content")) for m in fake.calls[1].messages if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_invalid_tool_arguments_feed_validation_error_to_model(tmp_path):
    runtime, fake = make_runtime([tool_turn("edit_file", {"new_text": "x"}), text_turn("I will add path")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    errors = events_of(session, "tool.error")
    assert errors and errors[0]["data"]["validation_error"] == "missing required field: path"
    assert errors[0]["tool_call_id"] == "tc_1"
    assert errors[0]["validation_error"] == "missing required field: path"
    assert errors[0]["duration_ms"] == 0
    assert not events_of(session, "tool.started")
    assert not events_of(session, "approval.requested")
    assert any("tool argument validation failed" in str(m.get("content")) for m in fake.calls[1].messages if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_denied_bash_feeds_reason_to_model(tmp_path):
    runtime, fake = make_runtime(
        [tool_turn("bash", {"command": "rm -rf /"}), text_turn("I will not delete it")], tmp_path
    )
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    denied = events_of(session, "tool.denied")
    assert denied and denied[0]["tool_call_id"] == "tc_1"
    second_call = fake.calls[1]
    assert any("denied by policy" in str(m.get("content")) for m in second_call.messages if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_edit_approval_flow_applies_after_accept(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, _ = make_runtime(
        [
            tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}),
            text_turn("Changed"),  # Response after verification injection.
            text_turn("Complete"),
        ],
        tmp_path,
    )
    session = make_session(tmp_path)

    async def approve_soon():
        for _ in range(100):
            await asyncio.sleep(0.01)
            pending = [a for a in session.approvals.values() if a.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=True)
                return

    _task = asyncio.create_task(approve_soon())
    await run_turn(session, Request(tmp_path), runtime)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"
    approvals = events_of(session, "approval.requested")
    assert approvals and approvals[0]["tool_call_id"] == "tc_1"
    applied_events = events_of(session, "edit.applied")
    assert applied_events
    assert applied_events[0]["tool_call_id"] == "tc_1"
    assert applied_events[0]["patch_hash"] == stable_hash(approvals[0]["diff"])
    assert isinstance(applied_events[0]["duration_ms"], int)
    await runtime.trace.flush()
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    applied_audit = next(record for record in records if record["event_type"] == "edit.applied")
    assert applied_audit["data"]["patch_hash"] == stable_hash(approvals[0]["diff"])
    assert "diff" not in applied_audit["data"]


@pytest.mark.asyncio
async def test_edit_rejected_reported_to_model(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, fake = make_runtime(
        [tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}), text_turn("Okay")],
        tmp_path,
    )
    session = make_session(tmp_path)

    async def reject_soon():
        for _ in range(100):
            await asyncio.sleep(0.01)
            pending = [a for a in session.approvals.values() if a.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=False)
                return

    _task = asyncio.create_task(reject_soon())
    await run_turn(session, Request(tmp_path), runtime)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    rejected = events_of(session, "edit.rejected")
    assert rejected and rejected[0]["tool_call_id"] == "tc_1"
    assert any("rejected" in str(m.get("content")) for m in fake.calls[1].messages if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_accept_all_skips_approval(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, _ = make_runtime(
        [
            tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}),
            text_turn("Changed"),
            text_turn("Complete"),
        ],
        tmp_path,
    )
    session = make_session(tmp_path)
    session.auto_accept_edits = True
    await run_turn(session, Request(tmp_path), runtime)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"
    auto_approved = events_of(session, "edit.auto_approved")
    assert auto_approved and auto_approved[0]["tool_call_id"] == "tc_1"
    assert not events_of(session, "approval.requested")


@pytest.mark.asyncio
async def test_verification_note_injected_after_edit(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, fake = make_runtime(
        [
            tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}),
            text_turn("Edit complete"),  # Attempted finish should trigger the verification note.
            text_turn("Verified"),
        ],
        tmp_path,
    )
    session = make_session(tmp_path)
    session.auto_accept_edits = True
    await run_turn(session, Request(tmp_path), runtime)
    assert len(fake.calls) == 3
    last_call = fake.calls[2]
    assert any("[system note]" in str(m.get("content")) for m in last_call.messages if m.get("role") == "user")


@pytest.mark.asyncio
async def test_review_mode_has_no_write_tools(tmp_path):
    runtime, fake = make_runtime([text_turn("Review complete")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path, mode="review"), runtime)
    tool_names = {t["name"] for t in fake.calls[0].tools}
    assert "edit_file" not in tool_names
    assert "bash" not in tool_names
    # Review-mode model calls use the reviewer route, not main.
    assert fake.calls[0].purpose == "reviewer"
    assert fake.calls[0].model == Settings().models.reviewer


@pytest.mark.asyncio
async def test_default_mode_uses_main_route(tmp_path):
    runtime, fake = make_runtime([text_turn("Complete")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    assert fake.calls[0].purpose == "main"
    assert fake.calls[0].model == Settings().models.main


@pytest.mark.asyncio
async def test_max_steps_forces_summary(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    turns = [tool_turn("read_file", {"path": "a.py"}, call_id=f"tc_{i}") for i in range(40)]
    turns.append(text_turn("Forced summary"))
    runtime, fake = make_runtime(turns, tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    assert fake.calls[-1].tools == []  # The final call has no tools.
    finals = events_of(session, "final")
    assert finals and "Forced summary" in finals[0]["summary"]


def budget_runtime(turns, tmp_path, *, max_total_tokens=0, max_total_cost=0.0):
    fake = FakeProvider(turns)
    settings = Settings()
    settings.budget.max_total_tokens = max_total_tokens
    settings.budget.max_total_cost = max_total_cost
    router = ModelRouter(primary=fake, settings=settings)
    return (
        AgentRuntime(
            model_runtime=router,
            trace=AuditLogger(path=tmp_path / "audit.jsonl"),
            policy=PolicyEngine(),
            tools=DefaultToolRuntime(),
            workspace=LocalWorkspaceRuntime(),
            clock=SystemClock(),
            approvals=SessionApprovalBroker(),
        ),
        fake,
    )


@pytest.mark.asyncio
async def test_token_budget_stops_a_runaway_turn(tmp_path):
    """A model looping over tool calls must be stopped by the cumulative cap.

    The script would keep calling list_files forever; the token budget has to cut
    it off and still hand the user a summary.
    """
    # Each scripted turn reports 15 tokens, so a 40-token cap trips after the
    # third call. Turn 4 is the wind-down; the 20 tool turns after it exist to
    # prove the loop really stopped instead of running to max_steps.
    turns = [tool_turn("list_files", {"path": "."}, call_id=f"tc_{i}") for i in range(3)]
    turns.append(text_turn("Stopped early and summarized."))
    turns.extend(tool_turn("list_files", {"path": "."}, call_id=f"extra_{i}") for i in range(20))
    runtime, fake = budget_runtime(turns, tmp_path, max_total_tokens=40)
    session = make_session(tmp_path)

    await run_turn(session, Request(tmp_path), runtime)

    exceeded = events_of(session, "run.budget.exceeded")
    assert len(exceeded) == 1
    assert exceeded[0]["reason"] == "tokens"
    assert exceeded[0]["total_tokens"] >= 40
    # The wind-down call must have happened, and it must be tool-free.
    assert fake.calls[-1].tools == []
    finals = events_of(session, "final")
    assert finals and finals[-1]["summary"] == "Stopped early and summarized."
    assert len(fake.calls) == 4
    assert len(fake.turns) == 20, "the loop must stop instead of consuming the remaining turns"


@pytest.mark.asyncio
async def test_cost_budget_stops_a_runaway_turn(tmp_path):
    turns = [tool_turn("list_files", {"path": "."}), text_turn("Wrapped up after the cost cap.")]
    turns.extend(tool_turn("list_files", {"path": "."}, call_id=f"extra_{i}") for i in range(10))
    runtime, fake = budget_runtime(turns, tmp_path, max_total_cost=0.0001)
    session = make_session(tmp_path)
    # Give the fake model a non-zero price so estimated_cost accumulates.
    runtime.model_runtime.settings.pricing.model_prices["fake/fake-model"] = ModelPrice(
        input_per_1m=1000.0, output_per_1m=1000.0
    )

    await run_turn(session, Request(tmp_path), runtime)

    exceeded = events_of(session, "run.budget.exceeded")
    assert len(exceeded) == 1
    assert exceeded[0]["reason"] == "cost"
    assert fake.calls[-1].tools == []


@pytest.mark.asyncio
async def test_budget_wind_down_does_not_recurse(tmp_path):
    """The wind-down call itself consumes tokens; it must not trip the gate again
    and start a second wind-down."""
    turns = [tool_turn("list_files", {"path": "."}), text_turn("Summary after budget stop.")]
    runtime, fake = budget_runtime(turns, tmp_path, max_total_tokens=1)
    session = make_session(tmp_path)

    await run_turn(session, Request(tmp_path), runtime)

    assert len(events_of(session, "run.budget.exceeded")) == 1
    assert len(events_of(session, "final")) == 1
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_disabled_budget_leaves_behaviour_unchanged(tmp_path):
    """0 disables a cap; the turn must complete normally with no budget event."""
    turns = [text_turn("Done without any budget stop.")]
    runtime, fake = budget_runtime(turns, tmp_path, max_total_tokens=0, max_total_cost=0.0)
    session = make_session(tmp_path)

    await run_turn(session, Request(tmp_path), runtime)

    assert events_of(session, "run.budget.exceeded") == []
    assert len(fake.calls) == 1
    finals = events_of(session, "final")
    assert finals[-1]["summary"] == "Done without any budget stop."


@pytest.mark.asyncio
async def test_approval_timeout_is_reported_to_the_model_as_not_a_refusal(tmp_path):
    """The user-visible payoff of distinguishing timeout from rejection.

    With a collapsed result the model was told "user rejected this edit" for a
    request nobody saw, and could abandon a correct plan. The tool result must
    now say it was not a refusal, and must tell the model to stop rather than
    retry into another unattended prompt.
    """
    target = tmp_path / "calc.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    turns = [
        tool_turn(
            "edit_file",
            {"path": "calc.py", "old_text": "a - b", "new_text": "a + b"},
            call_id="edit_1",
        ),
        text_turn("Reported the pending approval to the user."),
    ]
    runtime, _fake = make_runtime(turns, tmp_path)
    # No approver task runs, so the request goes unanswered.
    runtime.approvals = SessionApprovalBroker(timeout_seconds=0.05)
    session = make_session(tmp_path)

    await run_turn(session, Request(tmp_path), runtime)

    tool_messages = [message for message in session.messages if message.get("role") == "tool"]
    assert tool_messages, "the timed-out edit must still produce a tool result"
    text = tool_messages[-1]["content"]
    assert "timed out" in text
    assert "not a refusal" in text
    assert "rejected" not in text

    rejected = events_of(session, "edit.rejected")
    assert rejected and rejected[-1]["resolution"] == "timed_out"
    expired = events_of(session, "approval.expired")
    assert expired and expired[-1]["reason"] == "timeout"
    # The edit must not have been applied.
    assert target.read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"


@pytest.mark.asyncio
async def test_explicit_rejection_still_reads_as_a_refusal(tmp_path):
    """Regression guard: the timeout wording must not leak into real rejections."""
    target = tmp_path / "calc.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    turns = [
        tool_turn(
            "edit_file",
            {"path": "calc.py", "old_text": "a - b", "new_text": "a + b"},
            call_id="edit_1",
        ),
        text_turn("Adjusted after the rejection."),
    ]
    runtime, _fake = make_runtime(turns, tmp_path)
    session = make_session(tmp_path)

    async def reject_pending():
        for _ in range(200):
            pending = [a for a in session.approvals.values() if a.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=False)
                return
            await asyncio.sleep(0.01)

    rejecter = asyncio.create_task(reject_pending())
    await run_turn(session, Request(tmp_path), runtime)
    await rejecter

    tool_messages = [message for message in session.messages if message.get("role") == "tool"]
    text = tool_messages[-1]["content"]
    assert "user rejected" in text
    assert "timed out" not in text
    rejected = events_of(session, "edit.rejected")
    assert rejected and rejected[-1]["resolution"] == "rejected"


def multi_tool_turn(calls, text=""):
    """A scripted turn returning several tool calls at once, as models do."""
    events = [StreamEvent(type="text_delta", text=text)] if text else []
    events.extend(
        StreamEvent(type="tool_call", tool_call=ToolCallRequest(id=call_id, name=name, arguments=args))
        for call_id, name, args in calls
    )
    events.append(StreamEvent(type="done", usage=Usage(10, 5), model="fake-model"))
    return events


class SlowToolRuntime(DefaultToolRuntime):
    """Wraps the real registry, making each call take a measurable amount of time."""

    def __init__(self, delay: float = 0.15) -> None:
        super().__init__()
        self.delay = delay
        self.concurrent = 0
        self.peak_concurrent = 0

    async def run(self, name, arguments, context):
        self.concurrent += 1
        self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        try:
            await asyncio.sleep(self.delay)
            return await super().run(name, arguments, context)
        finally:
            self.concurrent -= 1


@pytest.mark.asyncio
async def test_consecutive_read_only_calls_run_in_parallel(tmp_path):
    """Latency of a read batch should track the slowest call, not their sum."""
    for index in range(4):
        (tmp_path / f"f{index}.py").write_text(f"x = {index}\n", encoding="utf-8")
    turns = [
        multi_tool_turn([(f"tc_{i}", "read_file", {"path": f"f{i}.py"}) for i in range(4)]),
        text_turn("Read them all."),
    ]
    runtime, _fake = make_runtime(turns, tmp_path)
    tools = SlowToolRuntime(delay=0.15)
    runtime.tools = tools
    session = make_session(tmp_path)

    started = time.perf_counter()
    await run_turn(session, Request(tmp_path), runtime)
    elapsed = time.perf_counter() - started

    assert tools.peak_concurrent == 4, "the four reads must overlap"
    # Serial would be >= 0.6s; allow generous headroom for the model calls.
    assert elapsed < 0.45, f"reads did not overlap: {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_tool_results_keep_the_model_call_order(tmp_path):
    """Some providers pair tool results to calls positionally, so completion
    order would corrupt the next request."""
    for index in range(4):
        (tmp_path / f"f{index}.py").write_text(f"x = {index}\n", encoding="utf-8")

    class ReversedCompletionOrder(DefaultToolRuntime):
        async def run(self, name, arguments, context):
            # Later calls finish first.
            index = int(str(arguments.get("path", "f0.py"))[1])
            await asyncio.sleep(0.02 * (4 - index))
            return await super().run(name, arguments, context)

    turns = [
        multi_tool_turn([(f"tc_{i}", "read_file", {"path": f"f{i}.py"}) for i in range(4)]),
        text_turn("Done."),
    ]
    runtime, _fake = make_runtime(turns, tmp_path)
    runtime.tools = ReversedCompletionOrder()
    session = make_session(tmp_path)

    await run_turn(session, Request(tmp_path), runtime)

    tool_ids = [m["tool_call_id"] for m in session.messages if m.get("role") == "tool"]
    assert tool_ids == ["tc_0", "tc_1", "tc_2", "tc_3"]


@pytest.mark.asyncio
async def test_writes_are_not_overlapped_and_reads_keep_their_place_around_them(tmp_path):
    """Only *consecutive* read-only calls group, so a read the model placed after
    an edit still observes that edit."""
    target = tmp_path / "calc.py"
    target.write_text("value = 1\n", encoding="utf-8")
    turns = [
        multi_tool_turn(
            [
                ("tc_a", "read_file", {"path": "calc.py"}),
                ("tc_b", "edit_file", {"path": "calc.py", "old_text": "value = 1", "new_text": "value = 2"}),
                ("tc_c", "read_file", {"path": "calc.py"}),
            ]
        ),
        text_turn("Edited and re-read."),
        # An applied edit makes the loop inject the verify note and ask once more.
        text_turn("Verified."),
    ]
    runtime, _fake = make_runtime(turns, tmp_path)
    tools = SlowToolRuntime(delay=0.02)
    runtime.tools = tools
    session = make_session(tmp_path)

    async def approve_pending():
        for _ in range(300):
            pending = [a for a in session.approvals.values() if a.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=True)
                return
            await asyncio.sleep(0.01)

    approver = asyncio.create_task(approve_pending())
    await run_turn(session, Request(tmp_path), runtime)
    await approver

    assert tools.peak_concurrent == 1, "a write must never overlap with anything"
    tool_messages = [m for m in session.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["tc_a", "tc_b", "tc_c"]
    # The read placed after the edit sees the edited content.
    assert "value = 1" in tool_messages[0]["content"]
    assert "value = 2" in tool_messages[2]["content"]


def test_tool_grouping_only_batches_consecutive_read_only_calls(tmp_path):
    runtime, _fake = make_runtime([text_turn("noop")], tmp_path)
    calls = [
        ToolCallRequest(id="1", name="read_file", arguments={}),
        ToolCallRequest(id="2", name="search", arguments={}),
        ToolCallRequest(id="3", name="bash", arguments={}),
        ToolCallRequest(id="4", name="read_file", arguments={}),
        ToolCallRequest(id="5", name="list_files", arguments={}),
        ToolCallRequest(id="6", name="edit_file", arguments={}),
    ]

    groups = [[call.id for call in group] for group in consecutive_tool_groups(calls, runtime)]

    assert groups == [["1", "2"], ["3"], ["4", "5"], ["6"]]


def test_unknown_tools_are_never_grouped(tmp_path):
    """An unregistered name has no spec, so it cannot be assumed side-effect free."""
    runtime, _fake = make_runtime([text_turn("noop")], tmp_path)
    calls = [
        ToolCallRequest(id="1", name="read_file", arguments={}),
        ToolCallRequest(id="2", name="mystery_tool", arguments={}),
        ToolCallRequest(id="3", name="read_file", arguments={}),
    ]

    groups = [[call.id for call in group] for group in consecutive_tool_groups(calls, runtime)]

    assert groups == [["1"], ["2"], ["3"]]
