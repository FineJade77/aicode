import asyncio
from pathlib import Path

import pytest

from app.agent.loop import run_agent_safely
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
from app.models.provider import ModelResponse
from app.server.main import MessageRequest
from app.sessions.store import Session
from app.tools.router import ToolRouter


class UnconfiguredPrimary:
    def is_configured(self) -> bool:
        return False


class BrokenModelRouter:
    primary = UnconfiguredPrimary()

    async def complete(self, request):
        raise RuntimeError("model unavailable")


class ConfiguredPrimary:
    def is_configured(self) -> bool:
        return True


class CoderPatchModelRouter:
    primary = ConfiguredPrimary()

    def __init__(self) -> None:
        self.purposes: list[str] = []

    async def complete(self, request):
        self.purposes.append(request.purpose)
        if request.purpose == "planner":
            return ModelResponse(text='{"action":"finish","reason":"context collected"}', model="test-planner", provider="test")
        if request.purpose == "coder":
            return ModelResponse(
                text='{"action":"patch","operation":"replace","path":"README.md","old_text":"old value","new_text":"new value","reason":"update requested text"}',
                model="test-coder",
                provider="test",
            )
        return ModelResponse(text="done", model="test-summary", provider="test")


@pytest.mark.asyncio
async def test_run_agent_safely_emits_final_on_model_error(tmp_path: Path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="hello", mode="default", workspace=str(tmp_path), language="zh-CN")
    runtime = AgentRuntime(
        model_router=BrokenModelRouter(),
        tools=ToolRouter(),
        audit=AuditLogger(tmp_path / "audit.jsonl"),
    )

    await run_agent_safely(session, request, runtime)

    events = []
    while not session.events.empty():
        events.append(await session.events.get())

    assert any(event["type"] == "error" for event in events)
    assert events[-1]["type"] == "final"
    assert "Agent 执行失败" in events[-1]["summary"]


@pytest.mark.asyncio
async def test_run_agent_uses_coder_patch_after_context_and_requires_approval(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("old value\n", encoding="utf-8")
    session = Session(session_id="sess_test", workspace=str(tmp_path), language="zh-CN")
    request = MessageRequest(message="修复 README.md 里的旧文案", mode="default", workspace=str(tmp_path), language="zh-CN")
    model_router = CoderPatchModelRouter()
    runtime = AgentRuntime(
        model_router=model_router,
        tools=ToolRouter(),
        audit=AuditLogger(tmp_path / "audit.jsonl"),
    )

    task = asyncio.create_task(run_agent_safely(session, request, runtime))
    events = []
    while True:
        event = await asyncio.wait_for(session.events.get(), timeout=5)
        events.append(event)
        if event["type"] == "approval.requested" and event.get("kind") == "patch":
            assert (tmp_path / "README.md").read_text(encoding="utf-8") == "old value\n"
            assert session.resolve_approval(event["approval_id"], accepted=True)
        if event["type"] == "final":
            break
    await asyncio.wait_for(task, timeout=5)

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "new value\n"
    assert "coder" in model_router.purposes
    assert any(event["type"] == "patch.preview" for event in events)
    assert any(event["type"] == "patch.applied" for event in events)
