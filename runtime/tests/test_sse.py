import asyncio

import pytest

from app.events.sse import encode_sse
from app.server import main as server
from app.sessions.store import Session, SessionEvents


def test_encode_sse_includes_event_id() -> None:
    encoded = encode_sse({"event_id": 7, "type": "final", "summary": "done"})

    assert encoded.startswith("id: 7\n")
    assert "event: final\n" in encoded
    assert '"event_id": 7' in encoded


@pytest.mark.asyncio
async def test_subscribe_yields_idle_ticks_instead_of_blocking_forever() -> None:
    """The idle tick is what lets a stream stay observably alive and re-check
    whether the run it follows can still emit."""
    events = SessionEvents()

    ticks = 0
    async for event in events.subscribe(after=0, idle_timeout=0.01):
        if event is None:
            ticks += 1
            if ticks == 2:
                break

    assert ticks == 2


@pytest.mark.asyncio
async def test_subscribe_still_blocks_when_no_idle_timeout_is_requested() -> None:
    """Existing callers keep the original semantics."""
    events = SessionEvents()
    subscription = events.subscribe(after=0)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.__anext__(), timeout=0.05)

    await subscription.aclose()


@pytest.mark.asyncio
async def test_final_event_for_run_finds_the_terminal_event() -> None:
    events = SessionEvents()
    events.set_current_run_id("run_a")
    await events.put({"type": "run.started"})
    await events.put({"type": "final", "summary": "done a"})
    events.set_current_run_id("run_b")
    await events.put({"type": "final", "summary": "done b"})

    assert events.final_event_for_run("run_a")["summary"] == "done a"
    assert events.final_event_for_run("run_b")["summary"] == "done b"
    assert events.final_event_for_run("run_missing") is None
    assert events.has_events_for_run("run_a") is True
    assert events.has_events_for_run("run_missing") is False


@pytest.mark.asyncio
async def test_unreachable_run_replays_a_final_the_client_already_passed(tmp_path) -> None:
    """A client that reconnected past its own `final` must still be terminated.

    The CLI treats a stream ending without `final` as a retryable disconnect, so
    without this it reconnects forever.
    """
    session = Session(session_id="sess_test", workspace=str(tmp_path))
    session.events.set_current_run_id("run_a")
    await session.events.put({"type": "final", "summary": "already finished"})
    final_id = session.events.last_event_id()

    terminal = server.unreachable_run_terminal(session, "run_a", cursor=final_id)

    assert terminal is not None
    assert terminal["summary"] == "already finished"
    # Before the cursor reaches the final, the stream must keep waiting for it.
    assert server.unreachable_run_terminal(session, "run_a", cursor=final_id - 1) is None


def test_unreachable_run_synthesizes_a_terminal_for_an_unknown_run(tmp_path) -> None:
    """Event persistence is best-effort, so a `final` can be missing after the
    session was evicted and rebuilt; a stale run id looks identical."""
    session = Session(session_id="sess_test", workspace=str(tmp_path))

    terminal = server.unreachable_run_terminal(session, "run_gone", cursor=0)

    assert terminal is not None
    assert terminal["type"] == "final"
    assert terminal["status"] == "unavailable"
    assert "run_gone" in terminal["summary"]
    # Synthesizing must not write anything to the session: nothing happened.
    assert session.events.events_after(0) == []


def test_unreachable_run_keeps_waiting_for_a_queued_run(tmp_path) -> None:
    """A run that has not started yet is "not yet", not "gone"."""
    session = Session(session_id="sess_test", workspace=str(tmp_path))
    queued = session.enqueue_agent_run(object())

    assert session.queued_run_ids() == {queued.run_id}
    assert server.unreachable_run_terminal(session, queued.run_id, cursor=0) is None


def test_unreachable_run_keeps_waiting_for_the_active_run(tmp_path) -> None:
    session = Session(session_id="sess_test", workspace=str(tmp_path))
    session.current_run_id = "run_active"

    assert server.unreachable_run_terminal(session, "run_active", cursor=0) is None
