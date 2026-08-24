import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.agent.policy import PolicyEngine
from app.agent.types import AgentRuntime
from app.application.contracts import TurnRequest
from app.application.errors import Conflict
from app.application.services import RunCoordinator
from app.audit.logger import AuditLogger
from app.config import Settings
from app.execution.models import ExecutionResult, ExecutionStatus
from app.models.router import ModelRouter
from app.project.detect import detect_test_command
from app.project.trust import TrustStore
from app.server import main as server
from app.server.main import (
    CreateSessionRequest,
    MessageRequest,
    SandboxExecutionRequest,
    TrustRequest,
    bind_message_request_to_session,
    cancel_execution,
    cancel_run,
    create_session,
    daemon_status,
    execute_sandbox,
    get_trust,
    model_probe,
    model_routes,
    remove_project_trust,
    review_rules,
    trust_project,
)
from app.sessions.approvals import SessionApprovalBroker
from app.sessions.store import Session, SessionStore
from app.system import SystemClock
from app.tools.runtime import DefaultToolRuntime
from app.tools.workspace import LocalWorkspaceRuntime
from tests.fakes import FakeProvider, build_test_runtime, text_turn


def test_detect_test_command_for_go_work(tmp_path: Path) -> None:
    (tmp_path / "go.work").write_text("go 1.22\n\nuse ./cli\n", encoding="utf-8")

    assert detect_test_command(tmp_path) == "go test ./cli/..."


def test_message_request_is_bound_to_session_context(tmp_path: Path) -> None:
    runtime = build_test_runtime(tmp_path)
    session = Session(session_id="sess_test", workspace=str(tmp_path))
    request = MessageRequest(message="hello", mode="default", workspace=str(tmp_path))

    effective = bind_message_request_to_session(runtime, session, request)

    assert isinstance(effective, TurnRequest)
    assert effective.workspace == session.workspace
    assert "language" not in effective.to_dict()
    assert "language" not in MessageRequest.model_fields


@pytest.mark.asyncio
async def test_create_session_has_no_language_field(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    runtime = build_test_runtime(tmp_path, sessions=store)

    response = await create_session(CreateSessionRequest(workspace=str(tmp_path)), runtime)
    session = store.get(response.session_id)

    assert session is not None
    assert "language" not in session.to_dict()
    assert "language" not in CreateSessionRequest.model_fields


def test_message_request_rejects_workspace_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    runtime = build_test_runtime(tmp_path)
    session = Session(session_id="sess_test", workspace=str(repo))
    request = MessageRequest(message="hello", mode="default", workspace=str(other))

    with pytest.raises(HTTPException) as exc_info:
        bind_message_request_to_session(runtime, session, request)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_run_coordinator_serializes_queued_messages(tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path))
    first = MessageRequest(message="first", mode="default", workspace=str(tmp_path))
    second = MessageRequest(message="second", mode="default", workspace=str(tmp_path))
    first_run = session.enqueue_agent_run(first)
    second_run = session.enqueue_agent_run(second)
    active = 0
    seen: list[str] = []

    class RecordingLoop:
        async def run(self, target_session: Session, request: MessageRequest) -> None:
            nonlocal active
            active += 1
            assert active == 1
            seen.append(request.message)
            await target_session.events.put({"type": "final", "summary": request.message})
            active -= 1

    coordinator = RunCoordinator(
        ModelRouter(primary=FakeProvider([]), settings=Settings()),
        AuditLogger(path=tmp_path / "audit.jsonl"),
        RecordingLoop(),
    )
    coordinator.ensure_runner(session)
    assert session.agent_runner_task is not None
    await session.agent_runner_task

    finals = [event for event in session.events.events_after(0) if event["type"] == "final"]
    assert seen == ["first", "second"]
    assert [event["summary"] for event in finals] == ["first", "second"]
    assert [event["run_id"] for event in finals] == [first_run.run_id, second_run.run_id]


@pytest.mark.asyncio
async def test_run_coordinator_queues_steer_for_active_run(tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path))
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingLoop:
        async def run(self, target_session: Session, _request: TurnRequest) -> None:
            started.set()
            await release.wait()
            await target_session.events.put({"type": "final", "summary": "done"})

    coordinator = RunCoordinator(
        ModelRouter(primary=FakeProvider([]), settings=Settings()),
        AuditLogger(path=tmp_path / "audit.jsonl"),
        BlockingLoop(),
    )
    request = TurnRequest(message="first", mode="default", workspace=str(tmp_path))
    accepted = await coordinator.submit(session, request)
    await asyncio.wait_for(started.wait(), timeout=1)

    result = await coordinator.steer(session, "Modify tests only")

    assert result.to_dict() == {"status": "queued", "run_id": accepted.run_id, "pending": 1}
    assert session.to_dict()["agent"]["pending_steers"] == 1
    assert [event for event in session.events.events_after(0) if event["type"] == "run.steer.queued"]

    release.set()
    assert session.agent_runner_task is not None
    await session.agent_runner_task
    assert session.to_dict()["agent"]["pending_steers"] == 0


@pytest.mark.asyncio
async def test_run_coordinator_rejects_steer_when_idle(tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path))
    coordinator = RunCoordinator(
        ModelRouter(primary=FakeProvider([]), settings=Settings()),
        AuditLogger(path=tmp_path / "audit.jsonl"),
        object(),
    )

    with pytest.raises(Conflict):
        await coordinator.steer(session, "too late")


@pytest.mark.asyncio
async def test_cancel_run_stops_current_and_continues_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    runtime = build_test_runtime(tmp_path, sessions=store)
    session = store.create(workspace=str(tmp_path))
    first = MessageRequest(message="first", mode="default", workspace=str(tmp_path))
    second = MessageRequest(message="second", mode="default", workspace=str(tmp_path))
    first_run = session.enqueue_agent_run(first)
    second_run = session.enqueue_agent_run(second)
    first_started = asyncio.Event()
    seen: list[str] = []
    approval_ids: list[str] = []

    class CancellableLoop:
        async def run(self, target_session: Session, request: MessageRequest) -> None:
            seen.append(request.message)
            if request.message == "first":
                approval = target_session.create_approval("tool", {"tool": "bash"})
                approval_ids.append(approval.approval_id)
                first_started.set()
                await target_session.wait_for_approval(approval.approval_id)
            await target_session.events.put({"type": "final", "summary": request.message})

    coordinator = RunCoordinator(
        ModelRouter(primary=FakeProvider([]), settings=Settings()),
        runtime.trace,
        CancellableLoop(),
    )
    runtime.runs = coordinator
    coordinator.ensure_runner(session)
    await asyncio.wait_for(first_started.wait(), timeout=1)

    response = await cancel_run(session.session_id, runtime)
    assert response == {"status": "cancelled", "run_id": first_run.run_id, "queued": 1}

    assert session.agent_runner_task is not None
    await asyncio.wait_for(session.agent_runner_task, timeout=1)

    events = session.events.events_after(0)
    cancelled = [event for event in events if event["type"] == "run.cancelled"]
    expired = [event for event in events if event["type"] == "approval.expired"]
    finals = [event for event in events if event["type"] == "final"]
    assert seen == ["first", "second"]
    assert cancelled[0]["run_id"] == first_run.run_id
    assert expired[0]["approval_id"] == approval_ids[0]
    assert session.approvals[approval_ids[0]].accepted is False
    assert [(event["run_id"], event.get("status")) for event in finals] == [
        (first_run.run_id, "cancelled"),
        (second_run.run_id, None),
    ]
    assert session.to_dict()["agent"]["running"] is False


@pytest.mark.asyncio
async def test_cancel_run_is_idempotent_when_session_is_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    runtime = build_test_runtime(tmp_path, sessions=store)
    session = store.create(workspace=str(tmp_path))

    response = await cancel_run(session.session_id, runtime)

    assert response == {"status": "idle", "run_id": None, "queued": 0}


@pytest.mark.asyncio
async def test_queued_run_history_does_not_include_future_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    runtime = build_test_runtime(tmp_path, sessions=store)
    fake = FakeProvider([text_turn("first done"), text_turn("second done")])
    agent = AgentRuntime(
        model_runtime=ModelRouter(primary=fake, settings=Settings()),
        trace=AuditLogger(path=tmp_path / "agent-audit.jsonl"),
        policy=PolicyEngine(),
        tools=DefaultToolRuntime(),
        workspace=LocalWorkspaceRuntime(),
        clock=SystemClock(),
        approvals=SessionApprovalBroker(),
    )
    runtime = build_test_runtime(tmp_path, sessions=store, agent=agent, model=agent.model_runtime)

    session = store.create(workspace=str(tmp_path))
    await server.send_message(
        session.session_id,
        MessageRequest(message="first", mode="default", workspace=str(tmp_path)),
        runtime,
    )
    await server.send_message(
        session.session_id,
        MessageRequest(message="second", mode="default", workspace=str(tmp_path)),
        runtime,
    )

    assert session.agent_runner_task is not None
    await session.agent_runner_task

    first_user_messages = [str(message.get("content")) for message in fake.calls[0].messages if message.get("role") == "user"]
    second_user_messages = [str(message.get("content")) for message in fake.calls[1].messages if message.get("role") == "user"]

    assert first_user_messages == ["first"]
    assert second_user_messages[-1] == "second"
    assert "first" in second_user_messages


@pytest.mark.asyncio
async def test_emit_run_queued_marks_queued_run(tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path))
    request = MessageRequest(message="hello", mode="default", workspace=str(tmp_path))
    queued = session.enqueue_agent_run(request)

    await RunCoordinator._emit_queued(session, queued, was_running=True, queue_position=2)

    event = await asyncio.wait_for(session.events.get(), timeout=1)
    assert event["type"] == "run.queued"
    assert event["run_id"] == queued.run_id
    assert event["status"] == "queued"
    assert event["queue_position"] == 2


@pytest.mark.asyncio
async def test_review_rules_endpoint_uses_workspace_config(tmp_path: Path) -> None:
    config_dir = tmp_path / ".aicode"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        '{"review":{"disabledRules":["large_diff","old_rule"],"largeDiffThreshold":1200,"maxFindings":25}}',
        encoding="utf-8",
    )

    data = await review_rules(build_test_runtime(tmp_path), str(tmp_path))
    rules = {rule["id"]: rule for rule in data["rules"]}

    assert data["effective_config"]["large_diff_threshold"] == 1200
    assert data["effective_config"]["max_findings"] == 25
    assert rules["large_diff"]["enabled"] is False
    assert data["config_warnings"][0]["rule"] == "old_rule"


@pytest.mark.asyncio
async def test_model_routes_endpoint_returns_route_status(tmp_path: Path) -> None:
    from app.models.openai_compatible import OpenAICompatibleProvider

    settings = Settings()
    runtime = build_test_runtime(
        tmp_path,
        model=ModelRouter(primary=OpenAICompatibleProvider(settings.openai_compatible), settings=settings),
    )

    data = await model_routes(runtime)

    assert data["provider"]["primary"] == "openai_compatible"
    assert data["provider"]["type"] == "openai_compatible"
    assert "main" in data["routes"]
    assert "reviewer" in data["routes"]
    assert "summarizer" in data["routes"]
    assert "api_key_env" in data["openai_compatible"]


@pytest.mark.asyncio
async def test_model_probe_endpoint_delegates_options(tmp_path: Path) -> None:
    class ProbeRouter:
        async def probe(self, *, model, tools):
            return {"status": "ok", "model": model, "tools": tools}

    runtime = build_test_runtime(tmp_path, model=ProbeRouter())

    data = await model_probe(runtime, tools=False, model="local-coder")

    assert data == {"status": "ok", "model": "local-coder", "tools": False}


@pytest.mark.asyncio
async def test_daemon_status_includes_event_writer_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    runtime = build_test_runtime(tmp_path, sessions=store)

    data = await daemon_status(runtime)

    assert data["status"] == "ok"
    assert data["audit_writer"]["queue_size"] == 0
    assert data["audit_writer"]["failed"] == 0
    assert data["event_writer"]["queue_size"] == 0
    assert data["event_writer"]["dropped"] == 0
    assert "active" in data["executions"]


@pytest.mark.asyncio
async def test_sandbox_execution_endpoint_uses_runtime_backend(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.test/demo\n", encoding="utf-8")
    fake = FakeExecutionService()
    runtime = build_test_runtime(tmp_path, execution=fake)

    result = await execute_sandbox(
        SandboxExecutionRequest(
            execution_id="exec_api",
            backend="docker",
            action="test",
            workspace=str(tmp_path),
            timeout_seconds=60,
        ),
        runtime,
    )

    assert result["status"] == "succeeded"
    assert result["action"] == "test"
    assert fake.request is not None
    assert fake.request.shell_command == "go test ./..."
    assert fake.request.network == "none"
    assert fake.request.backend == "docker"


@pytest.mark.asyncio
async def test_cancel_execution_endpoint_is_idempotent(tmp_path: Path) -> None:
    fake = FakeExecutionService()
    runtime = build_test_runtime(tmp_path, execution=fake)

    response = await cancel_execution("exec_api", runtime)

    assert response == {"status": "cancelled", "execution_id": "exec_api"}


@pytest.mark.asyncio
async def test_project_trust_endpoints_store_state_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = TrustStore(tmp_path / "state" / "trust.json")
    runtime = build_test_runtime(tmp_path, trust=store)

    initial = await get_trust(runtime, str(workspace))
    trusted = await trust_project(TrustRequest(workspace=str(workspace)), runtime)
    listed = await get_trust(runtime)
    removed = await remove_project_trust(TrustRequest(workspace=str(workspace)), runtime)

    assert initial["level"] == "untrusted"
    assert trusted["level"] == "trusted"
    assert listed["projects"][0]["workspace"] == str(workspace.resolve())
    assert removed["level"] == "untrusted"
    assert removed["removed"] is True


class FakeExecutionService:
    def __init__(self) -> None:
        self.request = None

    async def execute(self, request):
        self.request = request
        return ExecutionResult(
            execution_id=request.execution_id,
            backend=request.backend,
            status=ExecutionStatus.SUCCEEDED,
            exit_code=0,
            stdout="ok\n",
        )

    async def cancel(self, _execution_id):
        return True

    async def cancel_all(self):
        return None

    def status(self):
        return {"active": 0}


# --- per-turn shell backend ---------------------------------------------------


def test_a_message_can_choose_its_shell_backend() -> None:
    """`/sandbox` is per session, so the choice travels with each message.

    Storing it on the session instead would outlive the intent: a backend picked
    for one risky command would silently still be in force ten turns later.
    """
    request = MessageRequest(
        message="go", mode="chat", workspace="/repo", bash_backend="docker"
    )

    contract = request.to_contract()

    assert contract.bash_backend == "docker"
    assert contract.to_dict()["bash_backend"] == "docker"


def test_a_message_without_a_backend_leaves_the_choice_alone() -> None:
    contract = MessageRequest(message="go", mode="chat", workspace="/repo").to_contract()

    assert contract.bash_backend is None
    assert "bash_backend" not in contract.to_dict()


def test_an_unknown_backend_is_refused_at_the_edge() -> None:
    """A typo must not silently fall through to the default.

    Accepting `dcoker` and running on the host is the failure this validation
    exists to prevent: the user asked for isolation and would not be told they
    did not get it.
    """
    with pytest.raises(ValidationError):
        MessageRequest(message="go", mode="chat", workspace="/repo", bash_backend="dcoker")
