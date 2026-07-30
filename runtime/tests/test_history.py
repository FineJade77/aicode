from dataclasses import replace

import pytest

from app.agent.history import (
    latest_valid_compaction,
    load_history,
    prepare_history_for_model,
    truncate_tool_output,
)
from app.agent.types import AgentRuntime
from app.application.services import ContextService
from app.audit.logger import AuditLogger
from app.config.settings import Settings
from app.models.provider import ProviderError
from app.models.router import ModelRouter
from app.sessions.store import COMPACTION_SCHEMA_VERSION, SessionStore


class FailingSummaryProvider:
    provider_name = "fake"

    def is_configured(self):
        return True

    async def stream_complete(self, request):
        if False:
            yield
        raise ProviderError("summary temporarily unavailable")


@pytest.fixture
def session(tmp_path):
    store = SessionStore(path=tmp_path / "s.sqlite")
    return store.create(workspace=str(tmp_path)), store


def test_load_history_converts_legacy(session, tmp_path):
    sess, store = session
    store.append_message(sess, {"message": "Fix a bug", "mode": "default", "workspace": str(tmp_path)})
    store.append_message(sess, {"role": "assistant", "content": "Okay"})
    history = load_history(sess)
    assert history[0] == {"role": "user", "content": "Fix a bug"}
    assert history[1]["role"] == "assistant"


def test_load_history_redacts_known_runtime_secret(session, monkeypatch):
    sess, store = session
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret-value")
    store.append_message(sess, {"role": "tool", "content": "provider-secret-value"})

    history = load_history(sess)

    assert history[0]["content"] == "[REDACTED]"


def test_truncate_tool_output_layers():
    long_text = "x" * 20_000
    truncated = truncate_tool_output("bash", long_text)
    assert len(truncated) < 9_000
    assert "output truncated" in truncated
    assert truncate_tool_output("read_file", long_text) == long_text


@pytest.mark.asyncio
async def test_persistent_compaction_is_reused_after_restart(tmp_path):
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(path=db_path)
    sess = store.create(workspace=str(tmp_path))
    for index in range(10):
        store.append_message(sess, {"role": "user", "content": f"goal-{index} " + "x" * 30_000})

    runtime = AgentRuntime(model_runtime=None, trace=None)
    projected = await prepare_history_for_model(
        runtime=runtime,
        session=sess,
        purpose="main",
        system="system",
        tools=[],
        max_tokens=8_192,
    )

    assert projected[0]["content"].startswith("[persistent history summary")
    assert len(sess.compactions) == 1
    original_count = len(sess.messages)
    sess.append_compaction(
        replace(
            sess.compactions[-1],
            compaction_id=None,
            schema_version=COMPACTION_SCHEMA_VERSION + 1,
            summary="future schema must be ignored",
        )
    )

    restored = SessionStore(path=db_path).get(sess.session_id)
    assert restored is not None
    assert len(restored.messages) == original_count
    assert load_history(restored) == projected
    restored_compaction = latest_valid_compaction(restored)
    assert restored_compaction is not None
    assert restored_compaction.schema_version == COMPACTION_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_compaction_boundary_keeps_tool_call_and_result_together(tmp_path):
    store = SessionStore(path=tmp_path / "sessions.sqlite")
    sess = store.create(workspace=str(tmp_path))
    for index in range(8):
        store.append_message(
            sess,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"tc_{index}", "name": "bash", "arguments": {"command": "true"}}],
            },
        )
        store.append_message(
            sess,
            {"role": "tool", "tool_call_id": f"tc_{index}", "content": "x" * 40_000},
        )

    projected = await prepare_history_for_model(
        runtime=AgentRuntime(model_runtime=None, trace=None),
        session=sess,
        purpose="main",
        system="system",
        tools=[],
        max_tokens=8_192,
    )
    entry = latest_valid_compaction(sess)

    assert entry is not None
    assert sess.messages[sess.message_ids.index(entry.end_message_id)]["role"] == "tool"
    assert projected[1]["role"] == "assistant"
    remaining_call_ids = {
        call["id"]
        for message in projected
        for call in message.get("tool_calls") or []
    }
    remaining_result_ids = {
        message["tool_call_id"]
        for message in projected
        if message.get("role") == "tool"
    }
    assert remaining_call_ids == remaining_result_ids


@pytest.mark.asyncio
async def test_unfinished_tool_call_is_not_compacted(tmp_path):
    store = SessionStore(path=tmp_path / "sessions.sqlite")
    sess = store.create(workspace=str(tmp_path))
    store.append_message(
        sess,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "unfinished", "name": "bash", "arguments": {"command": "true"}}],
        },
    )
    for index in range(10):
        store.append_message(sess, {"role": "user", "content": f"{index} " + "x" * 30_000})

    projected = await prepare_history_for_model(
        runtime=AgentRuntime(model_runtime=None, trace=None),
        session=sess,
        purpose="main",
        system="system",
        tools=[],
        max_tokens=8_192,
    )

    assert not sess.compactions
    assert projected[0]["tool_calls"][0]["id"] == "unfinished"


@pytest.mark.asyncio
async def test_consecutive_compactions_advance_projection(tmp_path):
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(path=db_path)
    sess = store.create(workspace=str(tmp_path))
    runtime = AgentRuntime(model_runtime=None, trace=None)
    for index in range(10):
        store.append_message(sess, {"role": "user", "content": f"first-{index} " + "a" * 30_000})
    await prepare_history_for_model(
        runtime=runtime, session=sess, purpose="main", system="s", tools=[], max_tokens=8_192
    )
    first = latest_valid_compaction(sess)
    assert first is not None

    for index in range(10):
        store.append_message(sess, {"role": "user", "content": f"second-{index} " + "b" * 30_000})
    second_projection = await prepare_history_for_model(
        runtime=runtime, session=sess, purpose="main", system="s", tools=[], max_tokens=8_192
    )
    second = latest_valid_compaction(sess)

    assert second is not None
    assert second.end_message_id > first.end_message_id
    assert second.start_message_id == first.start_message_id
    assert len(sess.compactions) == 2
    restored = SessionStore(path=db_path).get(sess.session_id)
    assert restored is not None
    assert load_history(restored) == second_projection


@pytest.mark.asyncio
async def test_summary_failure_keeps_source_log_and_uses_recoverable_fallback(tmp_path):
    store = SessionStore(path=tmp_path / "sessions.sqlite")
    sess = store.create(workspace=str(tmp_path))
    for index in range(8):
        store.append_message(sess, {"role": "user", "content": f"goal-{index}"})
    original = list(sess.messages)
    router = ModelRouter(primary=FailingSummaryProvider(), settings=Settings())
    runtime = AgentRuntime(model_runtime=router, trace=None)

    projected = await prepare_history_for_model(
        runtime=runtime,
        session=sess,
        purpose="main",
        system="system",
        tools=[],
        max_tokens=1_024,
        force=True,
    )

    assert sess.messages == original
    assert projected[0]["content"].startswith("[persistent history summary")
    assert sess.compactions[-1].provider == "builtin"
    event = [item for item in sess.events.events_after(0) if item["type"] == "context.budget"][-1]
    assert event["summary_mode"] == "fallback"
    assert event["summary_error"] == "ProviderError"


@pytest.mark.asyncio
async def test_manual_context_service_persists_compaction(tmp_path):
    store = SessionStore(path=tmp_path / "manual.sqlite")
    sess = store.create(workspace=str(tmp_path))
    for index in range(5):
        store.append_message(sess, {"role": "user", "content": f"constraint-{index}"})
    trace = AuditLogger(path=tmp_path / "audit.jsonl")
    service = ContextService(AgentRuntime(model_runtime=None, trace=trace), trace)

    result = await service.compact(sess)

    assert result.status == "compacted"
    assert result.compaction is not None
    assert result.compaction["session_id"] == sess.session_id
    assert latest_valid_compaction(sess) is not None
