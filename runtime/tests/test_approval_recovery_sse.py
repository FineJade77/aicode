"""Pending-approval recovery, observed the way a client observes it.

`test_sessions.py` already pins the store-level half: a restart turns an
unresolved `approval.requested` into `approval.expired` plus the matching
rejection, a resolved one is left alone, and a second restart does not append
the pair twice. What none of that proves is that the recovered events reach a
subscriber — the CLI learns a pending approval is dead only from the event
stream, so a recovery that stays inside SQLite leaves the user watching a stream
that never resolves.

These tests build a second `ApplicationRuntime` against the database the first
one left behind, which is what a daemon restart is, and drive the SSE endpoint
to read what a reconnecting client would see.

**Not covered here: the socket.** The stream is driven by calling the endpoint
and draining its body iterator rather than over HTTP, because a recovered
session emits no `final` and therefore never ends — and httpx's ASGI transport
buffers a whole response body before returning it, so an endless stream can only
deadlock. What that leaves untested is the transport itself; what it keeps under
test is the part that was actually missing — cursor handling, event ordering and
the SSE framing a client parses.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.bootstrap import build_application_runtime
from app.config import Settings
from app.sessions.store import SessionStore

# Long enough to absorb scheduling jitter, short enough that a stream which
# stops producing fails the test instead of hanging the suite.
CHUNK_TIMEOUT = 5.0


async def seed_pending_approval(db_path: Path, kind: str) -> str:
    """Write a session whose approval never got an answer, then close the store.

    Written through the store rather than through the API because that is the
    state a daemon dies in: the request was emitted, the reply never came.
    """
    store = SessionStore(db_path)
    session = store.create(workspace="/repo")
    request: dict[str, Any] = {
        "type": "approval.requested",
        "approval_id": "appr_1",
        "kind": kind,
        "tool_call_id": "tc_1",
        "message": "Waiting for approval",
    }
    request.update({"tool": "bash"} if kind == "tool" else {"path": "a.py"})
    await session.events.put(request)
    await store.flush()
    return session.session_id


def restart():
    """The restart: a fresh runtime over the database the last one left behind.

    The path comes from `AICODE_SESSION_DB_PATH`, which the fixture points at a
    temporary file — `build_application_runtime` constructs its own store, so
    there is no seam to inject one through.
    """
    return build_application_runtime(Settings())


async def read_stream(runtime, session_id: str, *, expected: int) -> list[dict[str, Any]]:
    """Drain the SSE endpoint until `expected` events have been framed.

    Bounded by a count rather than by the stream ending: a recovered session has
    no `final`, so the endpoint correctly holds the connection open. A test that
    waited for it to close would hang instead of failing on its assertion.
    """
    from app.server import main as server

    response = await server.stream_events(
        session_id, request=_FakeRequest(), runtime=runtime, after=0
    )
    events: list[dict[str, Any]] = []
    iterator = response.body_iterator.__aiter__()
    try:
        while len(events) < expected:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=CHUNK_TIMEOUT)
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            for line in text.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: ") :]))
    finally:
        await response.body_iterator.aclose()
    return events


class _FakeRequest:
    """Only `headers` is read, for the `Last-Event-ID` reconnect cursor."""

    headers: dict[str, str] = {}


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sessions.sqlite"
    monkeypatch.setenv("AICODE_SESSION_DB_PATH", str(path))
    return path


@pytest.mark.asyncio
async def test_a_restart_tells_the_stream_the_tool_approval_died(db_path: Path) -> None:
    session_id = await seed_pending_approval(db_path, "tool")

    runtime = restart()
    try:
        events = await read_stream(runtime, session_id, expected=3)
    finally:
        await runtime.aclose()

    assert [event["type"] for event in events] == [
        "approval.requested",
        "approval.expired",
        "tool.rejected",
    ]
    assert events[1]["approval_id"] == "appr_1"
    # The rejection has to carry the call id, or a client cannot tell which
    # pending tool call it closes.
    assert events[2]["tool_call_id"] == "tc_1"


@pytest.mark.asyncio
async def test_a_restart_tells_the_stream_the_edit_approval_died(db_path: Path) -> None:
    session_id = await seed_pending_approval(db_path, "edit")

    runtime = restart()
    try:
        events = await read_stream(runtime, session_id, expected=3)
    finally:
        await runtime.aclose()

    assert [event["type"] for event in events] == [
        "approval.requested",
        "approval.expired",
        "edit.rejected",
    ]
    assert events[2]["path"] == "a.py"


@pytest.mark.asyncio
async def test_a_second_restart_does_not_replay_the_recovery(db_path: Path) -> None:
    """Two restarts must not leave a client with two rejections for one call.

    Pinned on the stream as well as in the store because a duplicate here is
    indistinguishable from a second, real rejection.
    """
    session_id = await seed_pending_approval(db_path, "tool")

    first = restart()
    try:
        await read_stream(first, session_id, expected=3)
    finally:
        await first.aclose()

    second = restart()
    try:
        events = await read_stream(second, session_id, expected=3)
    finally:
        await second.aclose()

    assert [event["type"] for event in events] == [
        "approval.requested",
        "approval.expired",
        "tool.rejected",
    ]


@pytest.mark.asyncio
async def test_a_resolved_approval_is_left_alone_across_a_restart(db_path: Path) -> None:
    """The stream must not report a rejection for a call that already ran."""
    store = SessionStore(db_path)
    session = store.create(workspace="/repo")
    await session.events.put(
        {
            "type": "approval.requested",
            "approval_id": "appr_1",
            "kind": "tool",
            "tool": "bash",
            "tool_call_id": "tc_1",
            "message": "Waiting for approval",
        }
    )
    await session.events.put(
        {"type": "tool.output", "tool": "bash", "tool_call_id": "tc_1", "text": "ok"}
    )
    await store.flush()

    runtime = restart()
    try:
        events = await read_stream(runtime, session.session_id, expected=2)
    finally:
        await runtime.aclose()

    assert [event["type"] for event in events] == ["approval.requested", "tool.output"]
