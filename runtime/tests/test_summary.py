"""Structured compaction summaries and the carry-forward guarantee."""

import json

import pytest

from app.agent.history import prepare_history_for_model
from app.agent.summary import (
    MAX_ITEMS_PER_FIELD,
    StructuredSummary,
    merge_summaries,
    parse_structured_summary,
)
from app.agent.types import AgentRuntime
from app.config import Settings
from app.models.provider import StreamEvent, Usage
from app.models.router import ModelRouter
from app.sessions.store import COMPACTION_SCHEMA_VERSION, SessionStore, parse_structured_column

VALID = {
    "goal": "Fix the adder",
    "constraints": ["do not touch .env"],
    "done": ["read calc.py"],
    "pending": ["run the test suite"],
    "files_touched": ["calc.py"],
    "open_failures": ["test_add fails"],
}


def test_parses_a_well_formed_object():
    parsed = parse_structured_summary(json.dumps(VALID))
    assert parsed is not None
    assert parsed.goal == "Fix the adder"
    assert parsed.pending == ["run the test suite"]
    assert parsed.open_failures == ["test_add fails"]


def test_parses_a_fenced_object():
    text = "```json\n" + json.dumps(VALID) + "\n```"
    parsed = parse_structured_summary(text)
    assert parsed is not None and parsed.goal == "Fix the adder"


def test_parses_an_object_with_surrounding_prose():
    text = "Here is the summary:\n" + json.dumps(VALID) + "\nHope that helps."
    parsed = parse_structured_summary(text)
    assert parsed is not None and parsed.pending == ["run the test suite"]


def test_a_string_where_a_list_belongs_is_normalised():
    """Accepted deliberately: discarding an otherwise usable summary over one
    mistyped field loses far more than it protects."""
    parsed = parse_structured_summary(json.dumps({**VALID, "pending": "run the tests"}))
    assert parsed is not None and parsed.pending == ["run the tests"]


def test_a_partial_object_is_accepted():
    parsed = parse_structured_summary(json.dumps({"goal": "Fix it"}))
    assert parsed is not None and parsed.goal == "Fix it" and parsed.pending == []


@pytest.mark.parametrize(
    "text",
    ["", "   ", "not json at all", "[1, 2, 3]", "null", json.dumps({"unrelated": "keys"}), json.dumps({})],
)
def test_unusable_replies_are_rejected(text):
    assert parse_structured_summary(text) is None


def test_an_object_with_only_empty_fields_is_rejected():
    empty = {key: [] for key in ("constraints", "done", "pending", "files_touched", "open_failures")}
    assert parse_structured_summary(json.dumps({"goal": "", **empty})) is None


def test_items_are_deduplicated_and_capped():
    payload = {"pending": [f"task {index % 3}" for index in range(200)]}
    parsed = parse_structured_summary(json.dumps(payload))
    assert parsed is not None
    assert parsed.pending == ["task 0", "task 1", "task 2"]

    payload = {"pending": [f"task {index}" for index in range(200)]}
    parsed = parse_structured_summary(json.dumps(payload))
    assert parsed is not None and len(parsed.pending) == MAX_ITEMS_PER_FIELD


def test_render_is_deterministic():
    parsed = parse_structured_summary(json.dumps(VALID))
    assert parsed is not None
    assert parsed.render() == parsed.render()
    assert "Pending:" in parsed.render()
    assert "- run the test suite" in parsed.render()


def test_merge_carries_unfinished_work_forward():
    """The core guarantee: the model does not get to silently drop pending work."""
    previous = StructuredSummary(pending=["run the suite"], open_failures=["test_add fails"])
    current = StructuredSummary(goal="Fix the adder", done=["edited calc.py"])

    merged = merge_summaries(previous, current)

    assert merged.pending == ["run the suite"]
    assert merged.open_failures == ["test_add fails"]
    assert merged.goal == "Fix the adder"


def test_merge_lets_an_item_be_resolved_through_done():
    previous = StructuredSummary(pending=["run the suite"], open_failures=["test_add fails"])
    current = StructuredSummary(done=["Run the suite", "TEST_ADD FAILS"])

    merged = merge_summaries(previous, current)

    assert merged.pending == []
    assert merged.open_failures == []


def test_merge_keeps_the_previous_goal_when_the_model_omits_it():
    merged = merge_summaries(StructuredSummary(goal="Fix the adder"), StructuredSummary(done=["x"]))
    assert merged.goal == "Fix the adder"


def test_merge_deduplicates_across_rounds():
    previous = StructuredSummary(files_touched=["calc.py"], pending=["run the suite"])
    current = StructuredSummary(files_touched=["calc.py"], pending=["run the suite"])
    merged = merge_summaries(previous, current)
    assert merged.files_touched == ["calc.py"]
    assert merged.pending == ["run the suite"]


def test_merge_without_a_previous_summary_is_the_current_one():
    current = StructuredSummary(goal="Fix it", pending=["a"])
    assert merge_summaries(None, current) is current


def test_stored_structure_round_trips():
    parsed = parse_structured_summary(json.dumps(VALID))
    assert parsed is not None
    restored = parse_structured_column(json.dumps(parsed.to_dict()))
    assert restored is not None and restored.to_dict() == parsed.to_dict()


@pytest.mark.parametrize("raw", [None, "", "not json", json.dumps([1, 2]), json.dumps("text")])
def test_a_malformed_stored_column_degrades_to_none(raw):
    """A summary is derived state; a bad row must not fail the session load."""
    assert parse_structured_column(raw) is None


class SummarizerProvider:
    """Returns scripted summarizer replies; anything else is a plain text turn."""

    provider_name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.summarizer_calls = 0

    def is_configured(self):
        return True

    async def stream_complete(self, request):
        if request.purpose == "summarizer":
            self.summarizer_calls += 1
            reply = self.replies.pop(0) if self.replies else "{}"
            yield StreamEvent(type="text_delta", text=reply)
            yield StreamEvent(type="done", usage=Usage(20, 8), model="summary-model")
            return
        yield StreamEvent(type="text_delta", text="ok")
        yield StreamEvent(type="done", usage=Usage(10, 5), model="main-model")


async def compact(session, runtime):
    return await prepare_history_for_model(
        runtime=runtime,
        session=session,
        purpose="main",
        system="system",
        tools=[],
        max_tokens=8_192,
    )


def bulk(store, session, marker, count=10):
    for index in range(count):
        store.append_message(session, {"role": "user", "content": f"{marker}-{index} " + "x" * 30_000})


@pytest.mark.asyncio
async def test_pending_survives_two_consecutive_compactions(tmp_path):
    """The acceptance case, and the one free text could not be asserted on.

    The second summarizer reply deliberately omits `pending` and `open_failures`
    entirely, exactly as a degrading free-text summary would.
    """
    store = SessionStore(path=tmp_path / "s.sqlite")
    sess = store.create(workspace=str(tmp_path))
    provider = SummarizerProvider(
        [
            json.dumps(
                {
                    "goal": "Fix the adder",
                    "pending": ["run the full suite"],
                    "open_failures": ["test_add fails"],
                }
            ),
            json.dumps({"goal": "Fix the adder", "done": ["edited calc.py"]}),
        ]
    )
    runtime = AgentRuntime(model_runtime=ModelRouter(primary=provider, settings=Settings()), trace=None)

    bulk(store, sess, "first")
    await compact(sess, runtime)
    bulk(store, sess, "second")
    projected = await compact(sess, runtime)

    assert provider.summarizer_calls == 2
    summary = projected[0]["content"]
    assert "run the full suite" in summary, "pending work must not be lost across compactions"
    assert "test_add fails" in summary, "open failures must not be lost across compactions"
    entry = sess.compactions[-1]
    assert entry.structured is not None
    assert entry.structured.pending == ["run the full suite"]
    assert entry.structured.open_failures == ["test_add fails"]


@pytest.mark.asyncio
async def test_resolved_work_stops_being_carried_forward(tmp_path):
    store = SessionStore(path=tmp_path / "s.sqlite")
    sess = store.create(workspace=str(tmp_path))
    provider = SummarizerProvider(
        [
            json.dumps({"goal": "Fix the adder", "pending": ["run the full suite"]}),
            json.dumps({"goal": "Fix the adder", "done": ["run the full suite"]}),
        ]
    )
    runtime = AgentRuntime(model_runtime=ModelRouter(primary=provider, settings=Settings()), trace=None)

    bulk(store, sess, "first")
    await compact(sess, runtime)
    bulk(store, sess, "second")
    await compact(sess, runtime)

    entry = sess.compactions[-1]
    assert entry.structured is not None
    assert entry.structured.pending == []
    assert "run the full suite" in entry.structured.done


@pytest.mark.asyncio
async def test_an_invalid_structure_falls_back_and_is_labelled(tmp_path):
    store = SessionStore(path=tmp_path / "s.sqlite")
    sess = store.create(workspace=str(tmp_path))
    provider = SummarizerProvider(["I could not produce JSON, sorry."])
    runtime = AgentRuntime(model_runtime=ModelRouter(primary=provider, settings=Settings()), trace=None)

    bulk(store, sess, "only")
    await compact(sess, runtime)

    events = [event for event in sess.events.events_after(0) if event.get("type") == "context.budget"]
    fallbacks = [event for event in events if event.get("summary_mode") == "fallback"]
    assert fallbacks and fallbacks[-1]["summary_error"] == "invalid_structure"
    entry = sess.compactions[-1]
    assert entry.structured is None, "free text must not be stored under a structured schema version"
    assert entry.summary, "the deterministic summary still has to be produced"


@pytest.mark.asyncio
async def test_structure_survives_a_restart(tmp_path):
    db_path = tmp_path / "s.sqlite"
    store = SessionStore(path=db_path)
    sess = store.create(workspace=str(tmp_path))
    provider = SummarizerProvider([json.dumps({"goal": "Fix it", "pending": ["run the suite"]})])
    runtime = AgentRuntime(model_runtime=ModelRouter(primary=provider, settings=Settings()), trace=None)

    bulk(store, sess, "only")
    await compact(sess, runtime)

    restored = SessionStore(path=db_path).get(sess.session_id)
    assert restored is not None
    entry = restored.compactions[-1]
    assert entry.schema_version == COMPACTION_SCHEMA_VERSION
    assert entry.structured is not None and entry.structured.pending == ["run the suite"]
