import pytest

from app.agent.history import (
    HISTORY_TOKEN_BUDGET,
    compact_if_needed,
    estimate_tokens,
    load_history,
    truncate_tool_output,
)
from app.agent.types import AgentRuntime
from app.models.router import ModelRouter
from app.config.settings import Settings
from app.sessions.store import SessionStore
from tests.fakes import FakeProvider, text_turn


@pytest.fixture
def session(tmp_path):
    store = SessionStore(path=tmp_path / "s.sqlite")
    return store.create(workspace=str(tmp_path), language="zh-CN"), store


def test_load_history_converts_legacy(session, tmp_path):
    sess, store = session
    store.append_message(sess, {"message": "修个 bug", "mode": "default", "workspace": str(tmp_path), "language": "zh-CN"})
    store.append_message(sess, {"role": "assistant", "content": "好的"})
    history = load_history(sess)
    assert history[0] == {"role": "user", "content": "修个 bug"}
    assert history[1]["role"] == "assistant"


def test_truncate_tool_output_layers():
    long_text = "x" * 20_000
    truncated = truncate_tool_output("bash", long_text)
    assert len(truncated) < 9_000
    assert "已截断" in truncated
    assert truncate_tool_output("read_file", long_text) == long_text


@pytest.mark.asyncio
async def test_compact_replaces_old_tool_messages(session):
    sess, _store = session
    runtime = AgentRuntime(model_router=None, tools=None, audit=None)
    big = "y" * 40_000
    history = [{"role": "user", "content": "task"}]
    for index in range(12):
        history.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"tc_{index}", "name": "bash", "arguments": {}}]})
        history.append({"role": "tool", "tool_call_id": f"tc_{index}", "content": big})
    before = estimate_tokens(history)
    assert before > HISTORY_TOKEN_BUDGET
    compacted = await compact_if_needed(history, runtime, sess)
    assert estimate_tokens(compacted) < before
    assert "[工具输出已压缩" in compacted[2]["content"]  # 最老的 tool 消息被压缩
    assert compacted[-1]["content"] == big  # 最近消息保留


@pytest.mark.asyncio
async def test_compact_noop_under_budget(session):
    sess, _store = session
    runtime = AgentRuntime(model_router=None, tools=None, audit=None)
    history = [{"role": "user", "content": "hi"}]
    assert await compact_if_needed(history, runtime, sess) == history
