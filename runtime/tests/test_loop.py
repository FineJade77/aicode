import asyncio
import json

import pytest

from app.adapters.approvals import SessionApprovalBroker
from app.adapters.system import SystemClock
from app.adapters.tools import DefaultToolRuntime
from app.adapters.workspace import LocalWorkspaceRuntime
from app.agent.loop import complete_with_compaction, run_turn
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger, stable_hash
from app.config.settings import Settings
from app.models.provider import (
    TOOL_ARGUMENT_PARSE_ERROR_KEY,
    ContextOverflowError,
    StreamEvent,
    Usage,
)
from app.models.router import ModelRouter
from app.policy.engine import PolicyEngine
from app.project.trust import TrustStore
from app.sessions.store import SessionStore
from tests.fakes import FakeProvider, text_turn, tool_turn


class Request:
    def __init__(self, workspace, message="修复 bug", mode="default", language="zh-CN", model=None):
        self.workspace = str(workspace)
        self.message = message
        self.mode = mode
        self.language = language
        self.model = model


def make_runtime(turns, tmp_path):
    fake = FakeProvider(turns)
    router = ModelRouter(primary=fake, settings=Settings())
    audit = AuditLogger(path=tmp_path / "audit.jsonl")
    return (
        AgentRuntime(
            model_router=router,
            audit=audit,
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
    session = store.create(workspace=str(tmp_path), language="zh-CN")
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
            yield StreamEvent(type="text_delta", text="- 保留当前目标与验证结果")
            yield StreamEvent(type="done", usage=Usage(20, 8), model="summary-model")
            return
        self.main_calls += 1
        if self.main_calls <= self.failures:
            raise ContextOverflowError("maximum context length exceeded")
        yield StreamEvent(type="text_delta", text="恢复成功")
        yield StreamEvent(type="done", usage=Usage(20, 8), model="main-model")


@pytest.mark.asyncio
async def test_plain_text_turn_emits_final(tmp_path):
    runtime, _ = make_runtime([text_turn("没什么要改的")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    finals = events_of(session, "final")
    assert finals and "没什么要改的" in finals[0]["summary"]
    assert events_of(session, "assistant.delta")


@pytest.mark.asyncio
async def test_message_can_override_model_without_changing_route(tmp_path):
    runtime, fake = make_runtime([text_turn("使用覆盖模型")], tmp_path)
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
                    session.enqueue_steer("不要执行工具，先解释风险")
                yield event

    fake = SteeringProvider(
        [tool_turn("bash", {"command": "touch should-not-exist"}), text_turn("已按新约束调整")]
    )
    runtime = AgentRuntime(
        model_router=ModelRouter(primary=fake, settings=Settings()),
        audit=AuditLogger(path=tmp_path / "audit.jsonl"),
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
        "不要执行工具" in str(message.get("content"))
        for message in fake.calls[1].messages
        if message.get("role") == "user"
    )


@pytest.mark.asyncio
async def test_context_overflow_forces_one_compaction_retry(tmp_path):
    provider = OverflowThenTextProvider(failures=1)
    router = ModelRouter(primary=provider, settings=Settings())
    runtime = AgentRuntime(
        model_router=router,
        audit=AuditLogger(path=tmp_path / "audit.jsonl"),
        policy=PolicyEngine(),
    )
    store = SessionStore(path=tmp_path / "overflow.sqlite")
    session = store.create(workspace=str(tmp_path), language="zh-CN")
    for index in range(4):
        store.append_message(session, {"role": "user", "content": f"历史目标 {index}"})

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

    assert result.text == "恢复成功"
    assert provider.main_calls == 2
    assert len(session.compactions) == 1
    assert history[0]["content"].startswith("[持久化历史摘要")
    budget_event = events_of(session, "context.budget")[-1]
    assert budget_event["forced"] is True
    assert budget_event["reason"] == "provider_overflow"


@pytest.mark.asyncio
async def test_context_overflow_is_never_retried_more_than_once(tmp_path):
    provider = OverflowThenTextProvider(failures=99)
    router = ModelRouter(primary=provider, settings=Settings())
    runtime = AgentRuntime(
        model_router=router,
        audit=AuditLogger(path=tmp_path / "audit.jsonl"),
        policy=PolicyEngine(),
    )
    store = SessionStore(path=tmp_path / "overflow-twice.sqlite")
    session = store.create(workspace=str(tmp_path), language="zh-CN")
    for index in range(4):
        store.append_message(session, {"role": "user", "content": f"历史目标 {index}"})

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
        [tool_turn("read_file", {"path": "a.py"}), text_turn("读完了")], tmp_path
    )
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    output_events = events_of(session, "tool.output")
    assert output_events
    assert output_events[0]["tool_call_id"] == "tc_1"
    assert isinstance(output_events[0]["duration_ms"], int)
    started_events = events_of(session, "tool.started")
    assert started_events and started_events[0]["tool_call_id"] == "tc_1"
    # 第二次模型调用的 messages 里包含 tool 结果
    second_call = fake.calls[1]
    assert any(m.get("role") == "tool" and "x = 1" in str(m.get("content")) for m in second_call.messages)


@pytest.mark.asyncio
async def test_tool_output_exposes_command_observability_fields(tmp_path):
    runtime, _ = make_runtime([tool_turn("bash", {"command": "ls"}), text_turn("列完了")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    output = events_of(session, "tool.output")[0]
    assert output["tool_call_id"] == "tc_1"
    assert output["exit_code"] == 0
    assert output["data"]["exit_code"] == 0
    assert isinstance(output["duration_ms"], int)
    await runtime.audit.flush()
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    finished = [record for record in records if record["event_type"] == "tool.finished"]
    assert finished and finished[0]["data"]["tool_call_id"] == "tc_1"


@pytest.mark.asyncio
async def test_untrusted_project_command_requires_approval_in_agent_loop(tmp_path):
    runtime, fake = make_runtime(
        [tool_turn("bash", {"command": "pytest --version"}), text_turn("未执行")],
        tmp_path,
    )
    runtime.trust_store = TrustStore(tmp_path.parent / f"{tmp_path.name}-state" / "trust.json")
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
    assert "未信任" in approvals[0]["reason"]
    assert any("拒绝" in str(message.get("content")) for message in fake.calls[1].messages if message.get("role") == "tool")


@pytest.mark.asyncio
async def test_malformed_tool_arguments_feed_parse_error_to_model(tmp_path):
    runtime, fake = make_runtime(
        [
            tool_turn(
                "read_file",
                {TOOL_ARGUMENT_PARSE_ERROR_KEY: {"error": "Expecting value", "raw_arguments": "{bad", "truncated": False}},
            ),
            text_turn("我会重试合法 JSON"),
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
    assert any("工具参数解析失败" in str(m.get("content")) for m in second_call.messages if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_non_dict_tool_arguments_do_not_reach_policy(tmp_path):
    runtime, fake = make_runtime([tool_turn("read_file", [], call_id="tc_bad"), text_turn("我会重试")], tmp_path)
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
    runtime, fake = make_runtime([tool_turn("edit_file", {"new_text": "x"}), text_turn("我会补 path")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    errors = events_of(session, "tool.error")
    assert errors and errors[0]["data"]["validation_error"] == "缺少必填字段: path"
    assert errors[0]["tool_call_id"] == "tc_1"
    assert errors[0]["validation_error"] == "缺少必填字段: path"
    assert errors[0]["duration_ms"] == 0
    assert not events_of(session, "tool.started")
    assert not events_of(session, "approval.requested")
    assert any("工具参数校验失败" in str(m.get("content")) for m in fake.calls[1].messages if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_denied_bash_feeds_reason_to_model(tmp_path):
    runtime, fake = make_runtime(
        [tool_turn("bash", {"command": "rm -rf /"}), text_turn("那我不删了")], tmp_path
    )
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    denied = events_of(session, "tool.denied")
    assert denied and denied[0]["tool_call_id"] == "tc_1"
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
    approvals = events_of(session, "approval.requested")
    assert approvals and approvals[0]["tool_call_id"] == "tc_1"
    applied_events = events_of(session, "edit.applied")
    assert applied_events
    assert applied_events[0]["tool_call_id"] == "tc_1"
    assert applied_events[0]["patch_hash"] == stable_hash(approvals[0]["diff"])
    assert isinstance(applied_events[0]["duration_ms"], int)
    await runtime.audit.flush()
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    applied_audit = next(record for record in records if record["event_type"] == "edit.applied")
    assert applied_audit["data"]["patch_hash"] == stable_hash(approvals[0]["diff"])
    assert "diff" not in applied_audit["data"]


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
    rejected = events_of(session, "edit.rejected")
    assert rejected and rejected[0]["tool_call_id"] == "tc_1"
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
    auto_approved = events_of(session, "edit.auto_approved")
    assert auto_approved and auto_approved[0]["tool_call_id"] == "tc_1"
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
    # review 模式的模型调用走 reviewer 路由，而非 main
    assert fake.calls[0].purpose == "reviewer"
    assert fake.calls[0].model == Settings().models.reviewer


@pytest.mark.asyncio
async def test_default_mode_uses_main_route(tmp_path):
    runtime, fake = make_runtime([text_turn("完成")], tmp_path)
    session = make_session(tmp_path)
    await run_turn(session, Request(tmp_path), runtime)
    assert fake.calls[0].purpose == "main"
    assert fake.calls[0].model == Settings().models.main


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
