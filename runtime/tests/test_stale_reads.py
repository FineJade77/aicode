"""Compaction must not assert file content that has since changed.

Compaction treats `read_file` output as fact. If the file changed after that
read, the summary bakes in outdated content stated as present-tense truth, and
the model never sees the raw message again to notice.
"""

import hashlib

import pytest

from app.agent.history import (
    DELETED_READ_NOTE,
    STALE_READ_NOTE,
    invalidate_stale_reads,
    load_history,
    prepare_history_for_model,
)
from app.agent.turn import MESSAGE_META_KEY, tool_message
from app.agent.types import AgentRuntime
from app.sessions.store import SessionStore


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_result(tmp_path, name, tool_call_id="tc_read"):
    """A persisted read_file result carrying the hash it actually saw."""
    target = tmp_path / name
    body = target.read_text(encoding="utf-8")
    return tool_message(
        tool_call_id,
        f"{name} has 1 lines; showing 1-1\n1\t{body}",
        {"read": {"path": name, "hash": file_hash(target)}},
    )


@pytest.fixture
def session(tmp_path):
    store = SessionStore(path=tmp_path / "s.sqlite")
    return store.create(workspace=str(tmp_path)), store


def test_an_unchanged_file_keeps_its_content(session, tmp_path):
    sess, _store = session
    (tmp_path / "calc.py").write_text("return a + b", encoding="utf-8")
    messages = [read_result(tmp_path, "calc.py")]

    kept, stale = invalidate_stale_reads(messages, sess)

    assert stale == []
    assert "return a + b" in kept[0]["content"]


def test_a_file_changed_after_the_read_loses_its_content(session, tmp_path):
    sess, _store = session
    target = tmp_path / "calc.py"
    target.write_text("return a - b", encoding="utf-8")
    messages = [read_result(tmp_path, "calc.py")]
    target.write_text("return a + b", encoding="utf-8")

    kept, stale = invalidate_stale_reads(messages, sess)

    assert stale == ["calc.py"]
    assert kept[0]["content"] == STALE_READ_NOTE.format(path="calc.py")
    assert "return a - b" not in kept[0]["content"]


def test_the_agents_own_edit_counts_as_a_change(session, tmp_path):
    """The case the rolling `read_files` record cannot catch.

    `record_read` is called on write as well as on read, so after the agent edits
    a file its rolling entry already matches disk. Comparing against that record
    would report "unchanged" for the single most common way a read goes stale.
    """
    sess, _store = session
    target = tmp_path / "calc.py"
    target.write_text("return a - b", encoding="utf-8")
    messages = [read_result(tmp_path, "calc.py")]

    target.write_text("return a + b", encoding="utf-8")
    sess.record_read("calc.py", file_hash(target))  # what an applied edit does
    assert sess.read_hash("calc.py") == file_hash(target), "rolling record matches disk"

    _kept, stale = invalidate_stale_reads(messages, sess)
    assert stale == ["calc.py"], "must still be detected despite the rolling record agreeing"


def test_a_deleted_file_is_reported_as_deleted(session, tmp_path):
    sess, _store = session
    target = tmp_path / "gone.py"
    target.write_text("x = 1", encoding="utf-8")
    messages = [read_result(tmp_path, "gone.py")]
    target.unlink()

    kept, stale = invalidate_stale_reads(messages, sess)

    assert stale == ["gone.py"]
    assert kept[0]["content"] == DELETED_READ_NOTE.format(path="gone.py")


def test_messages_without_read_metadata_are_untouched(session, tmp_path):
    sess, _store = session
    messages = [
        {"role": "user", "content": "Fix the bug"},
        tool_message("tc_1", "exit=0"),
        {"role": "assistant", "content": "Done"},
    ]

    kept, stale = invalidate_stale_reads(messages, sess)

    assert stale == []
    assert kept == messages


def test_only_the_changed_file_loses_its_content(session, tmp_path):
    sess, _store = session
    (tmp_path / "a.py").write_text("a = 1", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 2", encoding="utf-8")
    messages = [read_result(tmp_path, "a.py", "tc_a"), read_result(tmp_path, "b.py", "tc_b")]
    (tmp_path / "b.py").write_text("b = 22", encoding="utf-8")

    kept, stale = invalidate_stale_reads(messages, sess)

    assert stale == ["b.py"]
    assert "a = 1" in kept[0]["content"]
    assert "b = 2" not in kept[1]["content"]


def test_metadata_never_reaches_the_provider(session, tmp_path):
    """It is persisted with the message but is Runtime-private.

    Providers reject unknown message keys, so a leak here is an outage, not a
    cosmetic problem.
    """
    sess, store = session
    (tmp_path / "calc.py").write_text("x = 1", encoding="utf-8")
    store.append_message(sess, read_result(tmp_path, "calc.py"))

    history = load_history(sess)

    assert history[0]["tool_call_id"] == "tc_read"
    assert MESSAGE_META_KEY not in history[0]
    assert all(MESSAGE_META_KEY not in message for message in history)


@pytest.mark.asyncio
async def test_compaction_summary_drops_content_of_a_since_edited_file(tmp_path):
    store = SessionStore(path=tmp_path / "s.sqlite")
    sess = store.create(workspace=str(tmp_path))
    target = tmp_path / "calc.py"
    target.write_text("SECRET_OLD_BODY = 1", encoding="utf-8")

    store.append_message(sess, {"role": "user", "content": "Fix calc"})
    store.append_message(
        sess,
        {"role": "assistant", "content": "", "tool_calls": [{"id": "tc_read", "name": "read_file", "arguments": {"path": "calc.py"}}]},
    )
    store.append_message(sess, read_result(tmp_path, "calc.py"))
    # The agent edits the file, then the history grows enough to force compaction.
    target.write_text("NEW_BODY = 2", encoding="utf-8")
    for index in range(10):
        store.append_message(sess, {"role": "user", "content": f"step-{index} " + "x" * 30_000})

    projected = await prepare_history_for_model(
        runtime=AgentRuntime(model_runtime=None, trace=None),
        session=sess,
        purpose="main",
        system="system",
        tools=[],
        max_tokens=8_192,
    )

    summary = projected[0]["content"]
    assert summary.startswith("[persistent history summary")
    assert "SECRET_OLD_BODY" not in summary, "stale content must not be asserted as current"
    assert "calc.py" in summary and "no longer current" in summary
    events = [event for event in sess.events.events_after(0) if event.get("type") == "context.budget"]
    assert any(event.get("stale_reads") == ["calc.py"] for event in events)


@pytest.mark.asyncio
async def test_compaction_summary_keeps_content_of_an_unchanged_file(tmp_path):
    store = SessionStore(path=tmp_path / "s.sqlite")
    sess = store.create(workspace=str(tmp_path))
    (tmp_path / "calc.py").write_text("STABLE_BODY = 1", encoding="utf-8")

    store.append_message(sess, {"role": "user", "content": "Explain calc"})
    store.append_message(
        sess,
        {"role": "assistant", "content": "", "tool_calls": [{"id": "tc_read", "name": "read_file", "arguments": {"path": "calc.py"}}]},
    )
    store.append_message(sess, read_result(tmp_path, "calc.py"))
    for index in range(10):
        store.append_message(sess, {"role": "user", "content": f"step-{index} " + "x" * 30_000})

    projected = await prepare_history_for_model(
        runtime=AgentRuntime(model_runtime=None, trace=None),
        session=sess,
        purpose="main",
        system="system",
        tools=[],
        max_tokens=8_192,
    )

    summary = projected[0]["content"]
    assert "STABLE_BODY" in summary, "an unchanged file must still be summarized normally"
    assert "no longer current" not in summary


@pytest.mark.asyncio
async def test_the_loop_records_provenance_on_a_real_read(tmp_path):
    """Covers the wiring, not just the helper.

    The helper tests build the metadata by hand; this one proves `read_file`
    actually produces it and that the loop persists it onto the message.
    """
    from app.agent.loop import run_turn
    from app.agent.policy import PolicyEngine
    from app.audit.logger import AuditLogger
    from app.config import Settings
    from app.models.router import ModelRouter
    from app.sessions.approvals import SessionApprovalBroker
    from app.system import SystemClock
    from app.tools.runtime import DefaultToolRuntime
    from app.tools.workspace import LocalWorkspaceRuntime
    from tests.fakes import FakeProvider, text_turn, tool_turn

    class Request:
        def __init__(self, workspace):
            self.workspace = str(workspace)
            self.message = "Look at calc.py"
            self.mode = "default"
            self.model = None

    target = tmp_path / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")
    fake = FakeProvider([tool_turn("read_file", {"path": "calc.py"}), text_turn("Read it.")])
    runtime = AgentRuntime(
        model_runtime=ModelRouter(primary=fake, settings=Settings()),
        trace=AuditLogger(path=tmp_path / "audit.jsonl"),
        policy=PolicyEngine(),
        tools=DefaultToolRuntime(),
        workspace=LocalWorkspaceRuntime(),
        clock=SystemClock(),
        approvals=SessionApprovalBroker(),
    )
    store = SessionStore(path=tmp_path / "s.sqlite")
    sess = store.create(workspace=str(tmp_path))

    await run_turn(sess, Request(tmp_path), runtime)

    tool_messages = [m for m in sess.messages if m.get("role") == "tool"]
    assert tool_messages, "the read must have produced a tool message"
    meta = tool_messages[0].get(MESSAGE_META_KEY)
    assert meta == {"read": {"path": "calc.py", "hash": file_hash(target)}}

    # And it must not have travelled to the provider on the follow-up call.
    assert len(fake.calls) == 2
    assert all(MESSAGE_META_KEY not in message for message in fake.calls[1].messages)
