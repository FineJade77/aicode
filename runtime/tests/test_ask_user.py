"""Mid-turn questions.

Without this the model has two exits — keep guessing, or stop. An approval
cannot close the gap: it answers "may I do this?", not "which of these do you
want?". The timeout case matters most, because "nobody answered" must never read
to the model as "the user said no".
"""

import asyncio

import pytest

from app.agent.loop import run_turn
from app.agent.policy import PolicyEngine
from app.agent.session import ApprovalDecision
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
from app.config import Settings
from app.models.router import ModelRouter
from app.sessions.approvals import SessionApprovalBroker
from app.sessions.store import SessionStore
from app.system import SystemClock
from app.tools.ask import DECLINED_TEXT, TIMED_OUT_TEXT, AskUserTool
from app.tools.base import ToolContext
from app.tools.registry import TOOL_SPECS_BY_NAME
from app.tools.runtime import DefaultToolRuntime
from app.tools.workspace import LocalWorkspaceRuntime
from tests.fakes import FakeProvider, text_turn, tool_turn


class Request:
    def __init__(self, workspace, mode="default"):
        self.workspace = str(workspace)
        self.message = "Add tests"
        self.mode = mode
        self.model = None


def make_runtime(turns, tmp_path, *, timeout_seconds=None):
    fake = FakeProvider(turns)
    broker = SessionApprovalBroker(timeout_seconds=timeout_seconds) if timeout_seconds else SessionApprovalBroker()
    return (
        AgentRuntime(
            model_runtime=ModelRouter(primary=fake, settings=Settings()),
            trace=AuditLogger(path=tmp_path / "audit.jsonl"),
            policy=PolicyEngine(),
            tools=DefaultToolRuntime(),
            workspace=LocalWorkspaceRuntime(),
            clock=SystemClock(),
            approvals=broker,
        ),
        fake,
    )


def make_session(tmp_path):
    return SessionStore(path=tmp_path / "s.sqlite").create(workspace=str(tmp_path))


def events_of(session, event_type):
    return [e for e in session.events.events_after(0) if e.get("type") == event_type]


def tool(tmp_path, session, broker):
    return AskUserTool(TOOL_SPECS_BY_NAME["ask_user"]), ToolContext(
        workspace=tmp_path, session=session, approvals=broker
    )


@pytest.mark.asyncio
async def test_an_answer_is_returned_to_the_model(tmp_path):
    session = make_session(tmp_path)
    broker = SessionApprovalBroker()
    ask, context = tool(tmp_path, session, broker)

    async def answer_soon():
        for _ in range(200):
            pending = [a for a in session.approvals.values() if a.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=True, response="pytest")
                return
            await asyncio.sleep(0.005)

    task = asyncio.create_task(answer_soon())
    result = await ask.run({"question": "Which framework?", "options": ["pytest", "unittest"]}, context)
    await task

    assert result.success
    assert "pytest" in result.text
    assert result.data == {"status": "answered", "answer": "pytest"}
    asked = events_of(session, "question.asked")
    assert asked and asked[0]["options"] == ["pytest", "unittest"]


@pytest.mark.asyncio
async def test_a_timeout_is_not_a_refusal(tmp_path):
    """The distinction T-032 drew for approvals, applied to questions.

    Telling the model "the user said no" when nobody answered would make it
    abandon an otherwise correct plan.
    """
    session = make_session(tmp_path)
    broker = SessionApprovalBroker(timeout_seconds=0.05)
    ask, context = tool(tmp_path, session, broker)

    result = await ask.run({"question": "Which framework?"}, context)

    assert result.success
    assert result.data["status"] == "timed_out"
    assert result.text == TIMED_OUT_TEXT
    assert "not a refusal" in result.text
    assert "declined" not in result.text.lower()
    expired = events_of(session, "approval.expired")
    assert expired and expired[0]["kind"] == "question"


@pytest.mark.asyncio
async def test_declining_reads_differently_from_timing_out(tmp_path):
    session = make_session(tmp_path)
    broker = SessionApprovalBroker()
    ask, context = tool(tmp_path, session, broker)

    async def decline_soon():
        for _ in range(200):
            pending = [a for a in session.approvals.values() if a.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=False)
                return
            await asyncio.sleep(0.005)

    task = asyncio.create_task(decline_soon())
    result = await ask.run({"question": "Which framework?"}, context)
    await task

    assert result.data["status"] == "declined"
    assert result.text == DECLINED_TEXT
    assert result.text != TIMED_OUT_TEXT


@pytest.mark.asyncio
async def test_an_empty_answer_is_not_treated_as_an_answer(tmp_path):
    session = make_session(tmp_path)
    broker = SessionApprovalBroker()
    ask, context = tool(tmp_path, session, broker)

    async def answer_blank():
        for _ in range(200):
            pending = [a for a in session.approvals.values() if a.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=True, response="   ")
                return
            await asyncio.sleep(0.005)

    task = asyncio.create_task(answer_blank())
    result = await ask.run({"question": "Which framework?"}, context)
    await task

    assert result.data["status"] == "empty"


@pytest.mark.asyncio
async def test_an_empty_question_is_rejected(tmp_path):
    session = make_session(tmp_path)
    ask, context = tool(tmp_path, session, SessionApprovalBroker())
    result = await ask.run({"question": "   "}, context)
    assert not result.success


@pytest.mark.asyncio
async def test_no_attached_user_fails_loudly(tmp_path):
    """Returning a plausible answer here would be indistinguishable from a real one."""
    ask = AskUserTool(TOOL_SPECS_BY_NAME["ask_user"])
    result = await ask.run({"question": "Which framework?"}, ToolContext(workspace=tmp_path))
    assert not result.success
    assert "no interactive user" in result.error


@pytest.mark.asyncio
async def test_options_are_cleaned_and_capped(tmp_path):
    session = make_session(tmp_path)
    broker = SessionApprovalBroker(timeout_seconds=0.05)
    ask, context = tool(tmp_path, session, broker)

    await ask.run(
        {"question": "Pick", "options": ["a", "a", {"bad": 1}, "  b  ", *[f"o{i}" for i in range(20)]]},
        context,
    )

    options = events_of(session, "question.asked")[0]["options"]
    assert options[:3] == ["a", "b", "o0"]
    assert len(options) <= 8


@pytest.mark.asyncio
async def test_the_tool_is_hidden_in_read_only_modes(tmp_path):
    runtime, _fake = make_runtime([text_turn("Reviewed")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path, mode="review"), runtime)
    names = {schema["name"] for schema in runtime.tools.schemas_for_mode("review")}
    assert "ask_user" not in names


@pytest.mark.asyncio
async def test_the_loop_asks_and_feeds_the_answer_back(tmp_path):
    """End to end through the real loop, including the broker wiring."""
    runtime, fake = make_runtime(
        [
            tool_turn("ask_user", {"question": "Which framework?", "options": ["pytest"]}, call_id="tc_q"),
            text_turn("Using pytest."),
        ],
        tmp_path,
    )
    session = make_session(tmp_path)

    async def answer_soon():
        for _ in range(400):
            pending = [a for a in session.approvals.values() if a.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=True, response="pytest")
                return
            await asyncio.sleep(0.005)

    task = asyncio.create_task(answer_soon())
    await run_turn(session, Request(tmp_path), runtime)
    await task

    assert events_of(session, "question.asked")
    second_call = fake.calls[1]
    assert any(
        m.get("role") == "tool" and "pytest" in str(m.get("content"))
        for m in second_call.messages
    ), "the answer must reach the model"
    finals = events_of(session, "final")
    assert finals and "pytest" in finals[0]["summary"]


@pytest.mark.asyncio
async def test_asking_needs_no_separate_approval(tmp_path):
    """The question *is* the interaction; gating it would ask twice."""
    runtime, _fake = make_runtime(
        [
            tool_turn("ask_user", {"question": "Which framework?"}, call_id="tc_q"),
            text_turn("Done."),
        ],
        tmp_path,
    )
    session = make_session(tmp_path)

    async def answer_soon():
        for _ in range(400):
            pending = [a for a in session.approvals.values() if a.accepted is None]
            if pending:
                session.resolve_approval(pending[0].approval_id, accepted=True, response="pytest")
                return
            await asyncio.sleep(0.005)

    task = asyncio.create_task(answer_soon())
    await run_turn(session, Request(tmp_path), runtime)
    await task

    assert not events_of(session, "approval.requested")


def test_resolution_records_the_answer(tmp_path):
    session = make_session(tmp_path)
    approval = session.create_approval("question", {"question": "Which?"})
    assert session.resolve_approval(approval.approval_id, accepted=True, resolution="answered", response="pytest")
    assert session.approvals[approval.approval_id].response == "pytest"
    assert session.approvals[approval.approval_id].resolution == "answered"


def test_a_question_cannot_be_answered_twice(tmp_path):
    session = make_session(tmp_path)
    approval = session.create_approval("question", {"question": "Which?"})
    assert session.resolve_approval(approval.approval_id, accepted=True, response="first")
    assert not session.resolve_approval(approval.approval_id, accepted=True, response="second")
    assert session.approvals[approval.approval_id].response == "first"


@pytest.mark.asyncio
async def test_a_cancelled_run_releases_a_pending_question(tmp_path):
    """Cancellation must not leave the turn blocked on an unanswered question."""
    session = make_session(tmp_path)
    broker = SessionApprovalBroker()
    ask, context = tool(tmp_path, session, broker)

    async def cancel_soon():
        for _ in range(200):
            if [a for a in session.approvals.values() if a.accepted is None]:
                session.expire_pending_approvals()
                return
            await asyncio.sleep(0.005)

    task = asyncio.create_task(cancel_soon())
    result = await ask.run({"question": "Which framework?"}, context)
    await task

    assert result.success
    assert result.data["status"] in {"declined", "timed_out"}


def test_decisions_stay_four_state():
    assert {member.value for member in ApprovalDecision} == {"accepted", "rejected", "timed_out", "missing"}
