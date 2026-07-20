import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.project.detect import detect_test_command
from app.server.main import (
    MessageRequest,
    bind_message_request_to_session,
    emit_run_queued,
    model_routes,
    process_session_runs,
    review_rules,
)
from app.sessions.store import Session


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
    assert data["provider"]["fallback"] == "stub"
    assert "reviewer" in data["routes"]
    assert "summarizer" in data["routes"]
    assert "api_key_env" in data["openai_compatible"]


