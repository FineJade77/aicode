from pathlib import Path

import pytest

from app.agent.loop import run_agent_safely
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
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
