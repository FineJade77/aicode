import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
from app.config.settings import Settings
from app.execution.models import ExecutionResult, ExecutionStatus
from app.models.router import ModelRouter
from app.policy.engine import PolicyEngine
from app.project.detect import detect_test_command
from app.project.trust import TrustStore
from app.server import main as server
from app.server.main import (
    CreateSessionRequest,
    MessageRequest,
    bind_message_request_to_session,
    cancel_execution,
    cancel_run,
    create_session,
    daemon_status,
    execute_sandbox,
    emit_run_queued,
    effective_session_language,
    model_routes,
    process_session_runs,
    review_rules,
    SandboxExecutionRequest,
    get_trust,
    remove_project_trust,
    trust_project,
    TrustRequest,
)
from app.sessions.store import Session
from app.sessions.store import SessionStore
from tests.fakes import FakeProvider, text_turn


def test_detect_test_command_for_go_work(tmp_path: Path) -> None:
    (tmp_path / "go.work").write_text("go 1.22\n\nuse ./cli\n", encoding="utf-8")

    assert detect_test_command(tmp_path) == "go test ./cli/..."


def test_message_request_is_bound_to_session_context(tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="hello", mode="default", workspace=str(tmp_path), language="en")

    effective = bind_message_request_to_session(session, request)

    assert effective.workspace == session.workspace
    assert effective.language == "zh-CN"
    assert request.language == "en"


def test_effective_session_language_prefers_project_default(tmp_path: Path) -> None:
    config_dir = tmp_path / ".aicode"
    config_dir.mkdir()
    (config_dir / "config.json").write_text('{"defaultLanguage":"en-US"}', encoding="utf-8")

    assert effective_session_language(str(tmp_path), "zh-CN") == "en-US"


def test_effective_session_language_falls_back_to_request(tmp_path: Path) -> None:
    assert effective_session_language(str(tmp_path), "en-US") == "en-US"


@pytest.mark.asyncio
async def test_create_session_uses_project_default_language(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".aicode"
    config_dir.mkdir()
    (config_dir / "config.json").write_text('{"defaultLanguage":"en-US"}', encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.sqlite")
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "audit", AuditLogger(path=tmp_path / "audit.jsonl"))

    response = await create_session(CreateSessionRequest(workspace=str(tmp_path), language="zh-CN"))
    session = store.get(response.session_id)

    assert session is not None
    assert session.language == "en-US"


def test_message_request_rejects_workspace_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    session = Session(session_id="sess_test", workspace=str(repo), language="zh-CN")
    request = MessageRequest(message="hello", mode="default", workspace=str(other), language="zh-CN")

    with pytest.raises(HTTPException) as exc_info:
        bind_message_request_to_session(session, request)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_process_session_runs_serializes_queued_messages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    first = MessageRequest(message="first", mode="default", workspace=str(tmp_path), language="zh-CN")
    second = MessageRequest(message="second", mode="default", workspace=str(tmp_path), language="zh-CN")
    first_run = session.enqueue_agent_run(first)
    second_run = session.enqueue_agent_run(second)
    active = 0
    seen: list[str] = []

    async def fake_run_agent(target_session: Session, request: MessageRequest) -> None:
        nonlocal active
        active += 1
        assert active == 1
        seen.append(request.message)
        await target_session.events.put({"type": "final", "summary": request.message})
        active -= 1

    monkeypatch.setattr("app.server.main.run_agent", fake_run_agent)

    await process_session_runs(session)

    finals = [event for event in session.events.events_after(0) if event["type"] == "final"]
    assert seen == ["first", "second"]
    assert [event["summary"] for event in finals] == ["first", "second"]
    assert [event["run_id"] for event in finals] == [first_run.run_id, second_run.run_id]


@pytest.mark.asyncio
async def test_cancel_run_stops_current_and_continues_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "audit", AuditLogger(path=tmp_path / "audit.jsonl"))
    session = store.create(workspace=str(tmp_path), language="zh-CN")
    first = MessageRequest(message="first", mode="default", workspace=str(tmp_path), language="zh-CN")
    second = MessageRequest(message="second", mode="default", workspace=str(tmp_path), language="zh-CN")
    first_run = session.enqueue_agent_run(first)
    second_run = session.enqueue_agent_run(second)
    first_started = asyncio.Event()
    seen: list[str] = []
    approval_ids: list[str] = []

    async def fake_run_agent(target_session: Session, request: MessageRequest) -> None:
        seen.append(request.message)
        if request.message == "first":
            approval = target_session.create_approval("tool", {"tool": "bash"})
            approval_ids.append(approval.approval_id)
            first_started.set()
            await target_session.wait_for_approval(approval.approval_id)
        await target_session.events.put({"type": "final", "summary": request.message})

    monkeypatch.setattr("app.server.main.run_agent", fake_run_agent)
    server.ensure_session_runner(session)
    await asyncio.wait_for(first_started.wait(), timeout=1)

    response = await cancel_run(session.session_id)
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
    monkeypatch.setattr(server, "store", store)
    session = store.create(workspace=str(tmp_path), language="zh-CN")

    response = await cancel_run(session.session_id)

    assert response == {"status": "idle", "run_id": None, "queued": 0}


@pytest.mark.asyncio
async def test_queued_run_history_does_not_include_future_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "audit", AuditLogger(path=tmp_path / "audit.jsonl"))
    monkeypatch.setattr(server, "ensure_session_runner", lambda session: None)

    fake = FakeProvider([text_turn("first done"), text_turn("second done")])
    runtime = AgentRuntime(
        model_router=ModelRouter(primary=fake, settings=Settings()),
        audit=AuditLogger(path=tmp_path / "agent-audit.jsonl"),
        policy=PolicyEngine(),
    )
    monkeypatch.setattr(server, "agent_runtime", runtime)

    session = store.create(workspace=str(tmp_path), language="zh-CN")
    await server.send_message(
        session.session_id,
        MessageRequest(message="first", mode="default", workspace=str(tmp_path), language="zh-CN"),
    )
    await server.send_message(
        session.session_id,
        MessageRequest(message="second", mode="default", workspace=str(tmp_path), language="zh-CN"),
    )

    await process_session_runs(session)

    first_user_messages = [str(message.get("content")) for message in fake.calls[0].messages if message.get("role") == "user"]
    second_user_messages = [str(message.get("content")) for message in fake.calls[1].messages if message.get("role") == "user"]

    assert first_user_messages == ["first"]
    assert second_user_messages[-1] == "second"
    assert "first" in second_user_messages


@pytest.mark.asyncio
async def test_emit_run_queued_marks_queued_run(tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="hello", mode="default", workspace=str(tmp_path), language="zh-CN")
    queued = session.enqueue_agent_run(request)

    await emit_run_queued(session, queued, was_running=True, queue_position=2)

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

    data = await review_rules(str(tmp_path))
    rules = {rule["id"]: rule for rule in data["rules"]}

    assert data["effective_config"]["large_diff_threshold"] == 1200
    assert data["effective_config"]["max_findings"] == 25
    assert rules["large_diff"]["enabled"] is False
    assert data["config_warnings"][0]["rule"] == "old_rule"


@pytest.mark.asyncio
async def test_model_routes_endpoint_returns_route_status() -> None:
    data = await model_routes()

    assert data["provider"]["primary"] == "openai_compatible"
    assert data["provider"]["type"] == "openai_compatible"
    assert "main" in data["routes"]
    assert "reviewer" in data["routes"]
    assert "summarizer" in data["routes"]
    assert "api_key_env" in data["openai_compatible"]


@pytest.mark.asyncio
async def test_daemon_status_includes_event_writer_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "audit", AuditLogger(path=tmp_path / "audit.jsonl"))

    data = await daemon_status()

    assert data["status"] == "ok"
    assert data["audit_writer"]["queue_size"] == 0
    assert data["audit_writer"]["failed"] == 0
    assert data["event_writer"]["queue_size"] == 0
    assert data["event_writer"]["dropped"] == 0
    assert "active" in data["executions"]


@pytest.mark.asyncio
async def test_sandbox_execution_endpoint_uses_runtime_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.test/demo\n", encoding="utf-8")
    fake = FakeExecutionService()
    monkeypatch.setattr(server, "execution_service", fake)

    result = await execute_sandbox(
        SandboxExecutionRequest(
            execution_id="exec_api",
            backend="docker",
            action="test",
            workspace=str(tmp_path),
            timeout_seconds=60,
        )
    )

    assert result["status"] == "succeeded"
    assert result["action"] == "test"
    assert fake.request is not None
    assert fake.request.shell_command == "go test ./..."
    assert fake.request.network == "none"
    assert fake.request.backend == "docker"


@pytest.mark.asyncio
async def test_cancel_execution_endpoint_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeExecutionService()
    monkeypatch.setattr(server, "execution_service", fake)

    response = await cancel_execution("exec_api")

    assert response == {"status": "cancelled", "execution_id": "exec_api"}


@pytest.mark.asyncio
async def test_project_trust_endpoints_store_state_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = TrustStore(tmp_path / "state" / "trust.json")
    monkeypatch.setattr(server, "trust_store", store)
    monkeypatch.setattr(server, "audit", AuditLogger(path=tmp_path / "audit.jsonl"))

    initial = await get_trust(str(workspace))
    trusted = await trust_project(TrustRequest(workspace=str(workspace)))
    listed = await get_trust()
    removed = await remove_project_trust(TrustRequest(workspace=str(workspace)))

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
