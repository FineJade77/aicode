"""FakeProvider 驱动的全链路冒烟：改文件 → 确认 → 验证 → final。"""
import asyncio

import pytest

from app.agent.loop import run_turn_safely
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
from app.config.settings import Settings
from app.models.router import ModelRouter
from app.policy.engine import PolicyEngine
from app.sessions.store import SessionStore
from tests.fakes import FakeProvider, text_turn, tool_turn


class Request:
    def __init__(self, workspace, message="修复 add", mode="default", language="zh-CN"):
        self.workspace = str(workspace)
        self.message = message
        self.mode = mode
        self.language = language


@pytest.mark.asyncio
async def test_full_fix_flow(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    turns = [
        tool_turn("read_file", {"path": "calc.py"}, call_id="tc_1", text="先读文件"),
        tool_turn("edit_file", {"path": "calc.py", "old_text": "return a - b", "new_text": "return a + b"}, call_id="tc_2"),
        tool_turn("bash", {"command": "cat calc.py"}, call_id="tc_3", text="验证一下"),
        text_turn("已修复 add 函数"),
        text_turn("已验证完毕"),  # After verify note injection, model responds again
    ]
    fake = FakeProvider(turns)
    router = ModelRouter(primary=fake, settings=Settings())
    audit = AuditLogger(path=tmp_path / "audit.jsonl")
    runtime = AgentRuntime(
        model_router=router,
        audit=audit,
        policy=PolicyEngine(),
    )
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path), language="zh-CN")

    # Append initial message to session so it's in history for multi-turn
    initial_request = Request(tmp_path)
    store.append_message(session, {"message": initial_request.message, "mode": initial_request.mode, "workspace": initial_request.workspace, "language": initial_request.language})

    async def approve_all_pending():
        """Background task that approves all pending edits."""
        try:
            while True:
                await asyncio.sleep(0.01)
                for approval in list(session.approvals.values()):
                    if approval.accepted is None:
                        session.resolve_approval(approval.approval_id, accepted=True)
        except asyncio.CancelledError:
            pass

    approver = asyncio.create_task(approve_all_pending())
    try:
        await run_turn_safely(session, Request(tmp_path), runtime)
    finally:
        approver.cancel()

    # 验证文件确实被改了
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"

    # 验证关键事件都出现了
    events = session.events.events_after(0)
    types = [e["type"] for e in events]
    for expected in ["tool.started", "approval.requested", "edit.applied", "usage.recorded", "final"]:
        assert expected in types, f"missing event {expected}"

    # 验证 final 事件的总结内容恰为验证轮（verify note 注入后）模型输出，
    # 而非编辑轮的文本——确保 final 绑定到正确的控制流轮次，能捕获"提前 finalize"回归。
    finals = [e for e in events if e["type"] == "final"]
    assert finals and finals[-1]["summary"] == "已验证完毕"

    # 多轮记忆：再来一条消息，history 应包含上一轮内容
    fake.turns.append(text_turn("基于上一轮继续"))
    store.append_message(session, {"message": "继续", "mode": "default", "workspace": str(tmp_path), "language": "zh-CN"})
    await run_turn_safely(session, Request(tmp_path, message="继续"), runtime)

    # 验证上一轮的消息出现在第二轮的模型调用中
    last_call = fake.calls[-1]
    assert any("修复 add" in str(m.get("content")) for m in last_call.messages if m.get("role") == "user")
