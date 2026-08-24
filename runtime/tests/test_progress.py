"""No-progress detection.

`max_steps` is one number for "change one line" and "refactor across six files",
so tuning it only trades one failure mode for the other. Repetition is the signal
that actually separates a long task from a stuck one.
"""

import pytest

from app.agent.loop import run_turn
from app.agent.policy import PolicyEngine
from app.agent.progress import (
    REPEATED_ACTION,
    REPEATED_FAILURE,
    StallTracker,
    action_signature,
)
from app.agent.turn import TurnBudget
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
from app.config import Settings
from app.models.provider import StreamEvent, ToolCallRequest, Usage
from app.models.router import ModelRouter
from app.sessions.approvals import SessionApprovalBroker
from app.sessions.store import SessionStore
from app.system import SystemClock
from app.tools.runtime import DefaultToolRuntime
from app.tools.workspace import LocalWorkspaceRuntime
from tests.fakes import FakeProvider, text_turn, tool_turn


def test_argument_order_does_not_disguise_a_repeat():
    assert action_signature("bash", {"a": 1, "b": 2}) == action_signature("bash", {"b": 2, "a": 1})


def test_different_tools_are_not_the_same_action():
    assert action_signature("bash", {"x": 1}) != action_signature("search", {"x": 1})


def test_unserialisable_arguments_do_not_raise():
    assert action_signature("bash", {"fn": object()})


def test_identical_calls_trip_the_limit():
    tracker = StallTracker(limit=3)
    for _ in range(2):
        tracker.observe("bash", {"command": "pytest"}, ok=True, output="exit=0")
        assert not tracker.should_stop()
    tracker.observe("bash", {"command": "pytest"}, ok=True, output="exit=0")
    assert tracker.should_stop()
    assert tracker.tripped_reason == REPEATED_ACTION


def test_a_different_call_resets_the_streak():
    tracker = StallTracker(limit=3)
    tracker.observe("bash", {"command": "a"}, ok=True, output="")
    tracker.observe("bash", {"command": "a"}, ok=True, output="")
    tracker.observe("bash", {"command": "b"}, ok=True, output="")
    tracker.observe("bash", {"command": "a"}, ok=True, output="")
    assert not tracker.should_stop()


def test_varying_arguments_still_trip_on_the_identical_failure():
    """The shape a single streak would miss: different calls, same wall."""
    tracker = StallTracker(limit=3)
    for index in range(3):
        tracker.observe(
            "edit_file",
            {"path": "a.py", "old_text": f"variant-{index}"},
            ok=False,
            output="[edit failed] old_text was not found",
        )
    assert tracker.should_stop()
    assert tracker.tripped_reason == REPEATED_FAILURE


def test_a_success_clears_the_failure_streak():
    tracker = StallTracker(limit=3)
    tracker.observe("bash", {"command": "a"}, ok=False, output="boom")
    tracker.observe("bash", {"command": "b"}, ok=False, output="boom")
    tracker.observe("bash", {"command": "c"}, ok=True, output="exit=0")
    tracker.observe("bash", {"command": "d"}, ok=False, output="boom")
    assert not tracker.should_stop()


def test_the_warning_precedes_the_stop():
    tracker = StallTracker(limit=5)
    warnings = []
    for _ in range(4):
        tracker.observe("bash", {"command": "pytest"}, ok=True, output="")
        warning = tracker.pending_warning()
        if warning is not None:
            warnings.append(warning)
    assert warnings == [(REPEATED_ACTION, tracker.warn_at)]
    assert not tracker.should_stop()


def test_a_streak_is_warned_about_only_once():
    """Repeating the warning would spend the remaining budget restating it."""
    tracker = StallTracker(limit=5)
    for _ in range(4):
        tracker.observe("bash", {"command": "pytest"}, ok=True, output="")
    assert tracker.pending_warning() is not None
    assert tracker.pending_warning() is None


def test_two_identical_calls_are_never_enough():
    tracker = StallTracker(limit=5)
    tracker.observe("bash", {"command": "git status"}, ok=True, output="")
    tracker.observe("bash", {"command": "git status"}, ok=True, output="")
    assert tracker.pending_warning() is None
    assert not tracker.should_stop()


@pytest.mark.parametrize("limit", [0, 1])
def test_the_check_can_be_disabled(limit):
    tracker = StallTracker(limit=limit)
    for _ in range(20):
        tracker.observe("bash", {"command": "pytest"}, ok=True, output="")
    assert not tracker.enabled
    assert not tracker.should_stop()
    assert tracker.pending_warning() is None


class StuckProvider:
    """A model that keeps issuing the same failing call until tools are withheld.

    Closer to the real failure than a fixed script: it would run to `max_steps`
    on its own, so anything that stops it earlier is the detector doing its job.
    """

    provider_name = "fake"

    def __init__(self, command="cat missing_file.txt"):
        self.command = command
        self.calls = []

    def is_configured(self):
        return True

    async def stream_complete(self, request):
        self.calls.append(request)
        if not request.tools:
            yield StreamEvent(type="text_delta", text="I kept running the same failing command.")
            yield StreamEvent(type="done", usage=Usage(10, 5), model="fake-model")
            return
        yield StreamEvent(
            type="tool_call",
            tool_call=ToolCallRequest(id=f"tc_{len(self.calls)}", name="bash", arguments={"command": self.command}),
        )
        yield StreamEvent(type="done", usage=Usage(10, 5), model="fake-model")


class Request:
    def __init__(self, workspace):
        self.workspace = str(workspace)
        self.message = "Fix it"
        self.mode = "default"
        self.model = None


def make_runtime(turns, tmp_path, settings=None):
    fake = FakeProvider(turns)
    return (
        AgentRuntime(
            model_runtime=ModelRouter(primary=fake, settings=settings or Settings()),
            trace=AuditLogger(path=tmp_path / "audit.jsonl"),
            policy=PolicyEngine(),
            tools=DefaultToolRuntime(),
            workspace=LocalWorkspaceRuntime(),
            clock=SystemClock(),
            approvals=SessionApprovalBroker(),
        ),
        fake,
    )


def events_of(session, event_type):
    return [e for e in session.events.events_after(0) if e.get("type") == event_type]


@pytest.mark.asyncio
async def test_a_repeating_failing_command_is_stopped_before_max_steps(tmp_path):
    """The acceptance case.

    A model that reruns the same failing command is bounded by repetition, not by
    the step budget — and it still ends in a summary rather than a silent stop.
    """
    provider = StuckProvider()
    runtime, _fake = make_runtime([], tmp_path)
    runtime.model_runtime = ModelRouter(primary=provider, settings=Settings())
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path))

    await run_turn(session, Request(tmp_path), runtime)

    stopped = [e for e in events_of(session, "run.no_progress") if e["stopped"]]
    assert stopped, "the turn must be stopped for lack of progress"
    assert stopped[0]["count"] == 5
    assert len(provider.calls) < 40, "it must be caught well before the step budget"

    finals = events_of(session, "final")
    assert finals and finals[0]["summary"], "the stop must still produce a summary"
    assert provider.calls[-1].tools == [], "the wind-down call offers no tools"


@pytest.mark.asyncio
async def test_the_agent_is_warned_before_it_is_stopped(tmp_path):
    runtime, _fake = make_runtime([], tmp_path)
    runtime.model_runtime = ModelRouter(primary=StuckProvider(), settings=Settings())
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path))

    await run_turn(session, Request(tmp_path), runtime)

    events = events_of(session, "run.no_progress")
    assert [e["stopped"] for e in events] == [False, True], "warn first, then stop"
    notes = [
        str(m.get("content"))
        for m in session.messages
        if m.get("role") == "user" and "[system note]" in str(m.get("content"))
    ]
    assert any("same" in note for note in notes), "the model must be told plainly"


@pytest.mark.asyncio
async def test_varied_work_is_never_interrupted(tmp_path):
    """Guards the false positive that would matter most: a legitimate long task."""
    for index in range(6):
        (tmp_path / f"f{index}.py").write_text(f"x = {index}\n", encoding="utf-8")
    turns = [
        tool_turn("read_file", {"path": f"f{index}.py"}, call_id=f"tc_{index}")
        for index in range(6)
    ]
    turns.append(text_turn("Read them all."))
    runtime, fake = make_runtime(turns, tmp_path)
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path))

    await run_turn(session, Request(tmp_path), runtime)

    assert not events_of(session, "run.no_progress")
    assert len(fake.calls) == 7


@pytest.mark.asyncio
async def test_zero_limit_restores_the_previous_behaviour(tmp_path):
    settings = Settings()
    settings.budget.max_repeated_actions = 0
    provider = StuckProvider()
    runtime, _fake = make_runtime([], tmp_path)
    runtime.model_runtime = ModelRouter(primary=provider, settings=settings)
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path))

    await run_turn(session, Request(tmp_path), runtime)

    assert not events_of(session, "run.no_progress")
    # With the check off, only the step budget stops it — the old behaviour.
    assert len(provider.calls) == TurnBudget().max_steps + 1
