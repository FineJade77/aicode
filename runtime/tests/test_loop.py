import asyncio

import pytest

from app.agent.loop import run_turn
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
from app.config.settings import Settings
from app.models.router import ModelRouter
from app.policy.engine import PolicyEngine
from app.sessions.store import SessionStore
from tests.fakes import FakeProvider, text_turn, tool_turn


class Request:
    def __init__(self, workspace, message="修复 bug", mode="default", language="zh-CN"):
        self.workspace = str(workspace)
        self.message = message
        self.mode = mode
        self.language = language


def make_runtime(turns, tmp_path):
    fake = FakeProvider(turns)
    router = ModelRouter(primary=fake, settings=Settings())
    audit = AuditLogger(path=tmp_path / "audit.jsonl")
    return AgentRuntime(model_router=router, audit=audit, policy=PolicyEngine()), fake


def make_session(tmp_path):
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path), language="zh-CN")
    return session


def events_of(session, event_type):
    return [e for e in session.events.events_after(0) if e.get("type") == event_type]


@pytest.mark.asyncio
async def test_plain_text_turn_emits_final(tmp_path):
    runtime, _ = make_runtime([text_turn("没什么要改的")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    finals = events_of(session, "final")
    assert finals and "没什么要改的" in finals[0]["summary"]
    assert events_of(session, "assistant.delta")


@pytest.mark.asyncio
async def test_tool_loop_executes_and_feeds_back(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, fake = make_runtime(
        [tool_turn("read_file", {"path": "a.py"}), text_turn("读完了")], tmp_path
    )
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    assert events_of(session, "tool.output")
    # 第二次模型调用的 messages 里包含 tool 结果
    second_call = fake.calls[1]
    assert any(m.get("role") == "tool" and "x = 1" in str(m.get("content")) for m in second_call.messages)


@pytest.mark.asyncio
async def test_denied_bash_feeds_reason_to_model(tmp_path):
    runtime, fake = make_runtime(
        [tool_turn("bash", {"command": "rm -rf /"}), text_turn("那我不删了")], tmp_path
    )
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    assert events_of(session, "tool.denied")
    second_call = fake.calls[1]
    assert any("被策略拒绝" in str(m.get("content")) for m in second_call.messages if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_edit_approval_flow_applies_after_accept(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, _ = make_runtime(
        [
            tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}),
            text_turn("已修改"),  # 验证注入后的回应
            text_turn("完成"),
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
    assert events_of(session, "approval.requested")
    assert events_of(session, "edit.applied")


@pytest.mark.asyncio
async def test_edit_rejected_reported_to_model(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, fake = make_runtime(
        [tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}), text_turn("好吧")],
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
    assert any("拒绝" in str(m.get("content")) for m in fake.calls[1].messages if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_accept_all_skips_approval(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, _ = make_runtime(
        [
            tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}),
            text_turn("已修改"),
            text_turn("完成"),
        ],
        tmp_path,
    )
    session = make_session(tmp_path)
    session.auto_accept_edits = True
    await run_turn(session, Request(tmp_path), runtime)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"
    assert events_of(session, "edit.auto_approved")
    assert not events_of(session, "approval.requested")


@pytest.mark.asyncio
async def test_verification_note_injected_after_edit(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    runtime, fake = make_runtime(
        [
            tool_turn("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}),
            text_turn("改完了"),  # 尝试结束 → 应被注入验证提示
            text_turn("验证过了"),
        ],
        tmp_path,
    )
    session = make_session(tmp_path)
    session.auto_accept_edits = True
    await run_turn(session, Request(tmp_path), runtime)
    assert len(fake.calls) == 3
    last_call = fake.calls[2]
    assert any("[系统提示]" in str(m.get("content")) for m in last_call.messages if m.get("role") == "user")


@pytest.mark.asyncio
async def test_review_mode_has_no_write_tools(tmp_path):
    runtime, fake = make_runtime([text_turn("审查完成")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path, mode="review"), runtime)
    tool_names = {t["name"] for t in fake.calls[0].tools}
    assert "edit_file" not in tool_names
    assert "bash" not in tool_names


@pytest.mark.asyncio
async def test_max_steps_forces_summary(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    turns = [tool_turn("read_file", {"path": "a.py"}, call_id=f"tc_{i}") for i in range(40)]
    turns.append(text_turn("被迫总结"))
    runtime, fake = make_runtime(turns, tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    assert fake.calls[-1].tools == []  # 最后一次调用不带工具
    finals = events_of(session, "final")
    assert finals and "被迫总结" in finals[0]["summary"]
