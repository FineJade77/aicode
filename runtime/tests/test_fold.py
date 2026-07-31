"""The middle context tier: fold bulk tool output before spending a summary.

Before this there were only two levels — per-tool truncation at write time, and a
lossy summary of the whole history once the threshold was crossed. Exceeding the
threshold by a little therefore cost a model call and discarded detail that
dropping old tool output alone would have recovered.
"""

import pytest

from app.agent.history import (
    FOLD_KEEP_RECENT_GROUPS,
    estimate_prompt_tokens,
    fold_old_tool_output,
    latest_valid_compaction,
    prepare_history_for_model,
)
from app.agent.types import AgentRuntime
from app.sessions.store import SessionStore


def call_group(index, tool="bash", output="x" * 40_000):
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"tc_{index}", "name": tool, "arguments": {}}],
        },
        {"role": "tool", "tool_call_id": f"tc_{index}", "content": output},
    ]


def history(groups):
    messages = []
    for index in range(groups):
        messages.extend(call_group(index))
    return messages


def test_recent_groups_keep_their_output_verbatim():
    messages = history(10)
    folded, count = fold_old_tool_output(messages, keep_recent_groups=4)

    assert count == 6
    kept = [m for m in folded[-8:] if m["role"] == "tool"]
    assert all(m["content"] == "x" * 40_000 for m in kept)
    older = [m for m in folded[:-8] if m["role"] == "tool"]
    assert all("folded to save context" in m["content"] for m in older)


def test_only_tool_messages_are_folded():
    """The basis of the zero-loss claim for anything that drives the next step."""
    messages = [
        {"role": "user", "content": "Do not touch .env. " + "u" * 40_000},
        {"role": "assistant", "content": "Plan: read then edit. " + "a" * 40_000},
        *call_group(0),
        *call_group(1),
        *call_group(2),
        *call_group(3),
        *call_group(4),
    ]
    folded, count = fold_old_tool_output(messages, keep_recent_groups=1)

    assert count >= 1
    assert folded[0]["content"] == messages[0]["content"], "user constraints must survive intact"
    assert folded[1]["content"] == messages[1]["content"], "assistant reasoning must survive intact"


def test_the_tool_name_is_named_in_the_reference():
    messages = [*call_group(0, tool="search"), *call_group(1), *call_group(2)]
    folded, _count = fold_old_tool_output(messages, keep_recent_groups=1)
    assert "search" in folded[1]["content"]


def test_an_unresolved_group_is_never_folded():
    """Same boundary rule compaction uses: a tool call and its result stay whole."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc_a", "name": "bash", "arguments": {}},
                {"id": "tc_b", "name": "bash", "arguments": {}},
            ],
        },
        {"role": "tool", "tool_call_id": "tc_a", "content": "x" * 40_000},
        *call_group(1),
        *call_group(2),
        *call_group(3),
        *call_group(4),
        *call_group(5),
    ]
    folded, _count = fold_old_tool_output(messages, keep_recent_groups=1)

    assert folded[1]["content"] == "x" * 40_000, "the incomplete group must be left alone"


def test_short_output_is_left_alone():
    """Folding a short result would cost more characters than it saves."""
    messages = [*call_group(0, output="ok"), *call_group(1), *call_group(2)]
    folded, count = fold_old_tool_output(messages, keep_recent_groups=1)
    assert folded[1]["content"] == "ok"
    assert count == 1


def test_nothing_to_fold_returns_the_original_list():
    messages = history(2)
    folded, count = fold_old_tool_output(messages, keep_recent_groups=FOLD_KEEP_RECENT_GROUPS)
    assert count == 0
    assert folded is messages


def test_folding_reduces_the_estimate():
    messages = history(10)
    folded, _count = fold_old_tool_output(messages, keep_recent_groups=2)
    assert estimate_prompt_tokens("system", folded, [], 3.5) < estimate_prompt_tokens("system", messages, [], 3.5)


@pytest.mark.asyncio
async def test_folding_alone_avoids_a_summary(tmp_path):
    """The acceptance case: at the middle threshold, fold and do not summarize."""
    store = SessionStore(path=tmp_path / "s.sqlite")
    sess = store.create(workspace=str(tmp_path))
    store.append_message(sess, {"role": "user", "content": "Fix the adder and do not touch .env"})
    for index in range(10):
        for message in call_group(index):
            store.append_message(sess, message)

    projected = await prepare_history_for_model(
        runtime=AgentRuntime(model_runtime=None, trace=None),
        session=sess,
        purpose="main",
        system="system",
        tools=[],
        max_tokens=8_192,
    )

    assert latest_valid_compaction(sess) is None, "no summary should have been produced"
    assert not any(str(m.get("content", "")).startswith("[persistent history summary") for m in projected)
    # Nothing that drives the next step was lost.
    assert projected[0]["content"] == "Fix the adder and do not touch .env"
    assert any("folded to save context" in str(m.get("content") or "") for m in projected)

    events = [e for e in sess.events.events_after(0) if e.get("type") == "context.budget"]
    fold_events = [e for e in events if e.get("reason") == "folded"]
    assert fold_events, "the fold must be reported"
    assert fold_events[-1]["compacted"] is False
    assert fold_events[-1]["folded_tool_outputs"] > 0
    assert fold_events[-1]["after_tokens"] < fold_events[-1]["before_tokens"]


@pytest.mark.asyncio
async def test_summary_still_happens_when_folding_is_not_enough(tmp_path):
    """Folding only drops tool output, so bulk elsewhere still reaches the summary."""
    store = SessionStore(path=tmp_path / "s.sqlite")
    sess = store.create(workspace=str(tmp_path))
    for index in range(10):
        store.append_message(sess, {"role": "user", "content": f"step-{index} " + "u" * 40_000})

    await prepare_history_for_model(
        runtime=AgentRuntime(model_runtime=None, trace=None),
        session=sess,
        purpose="main",
        system="system",
        tools=[],
        max_tokens=8_192,
    )

    assert latest_valid_compaction(sess) is not None


@pytest.mark.asyncio
async def test_a_history_under_the_limit_is_not_folded(tmp_path):
    store = SessionStore(path=tmp_path / "s.sqlite")
    sess = store.create(workspace=str(tmp_path))
    for index in range(6):
        for message in call_group(index, output="small output"):
            store.append_message(sess, message)

    projected = await prepare_history_for_model(
        runtime=AgentRuntime(model_runtime=None, trace=None),
        session=sess,
        purpose="main",
        system="system",
        tools=[],
        max_tokens=8_192,
    )

    assert all("folded to save context" not in str(m.get("content") or "") for m in projected)
    assert not [e for e in sess.events.events_after(0) if e.get("reason") == "folded"]
