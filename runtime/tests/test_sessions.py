import sqlite3
from pathlib import Path

import pytest

from app.sessions.store import SessionEvents, SessionStore, normalize_event_limit


def test_session_store_persists_sessions_and_messages(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo", language="zh-CN")
    store.append_message(session, {"message": "hello", "mode": "chat"})

    reloaded = SessionStore(db_path)
    restored = reloaded.get(session.session_id)

    assert restored is not None
    assert restored.session_id == session.session_id
    assert restored.workspace == "/repo"
    assert restored.messages == [{"message": "hello", "mode": "chat"}]


def test_session_store_lists_newest_first(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    first = store.create(workspace="/repo1", language="zh-CN")
    second = store.create(workspace="/repo2", language="zh-CN")

    sessions = store.list()

    assert sessions[0]["session_id"] == second.session_id
    assert sessions[1]["session_id"] == first.session_id


def test_session_store_lists_recently_updated_first(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    first = store.create(workspace="/repo1", language="zh-CN")
    second = store.create(workspace="/repo2", language="zh-CN")

    store.append_message(first, {"message": "resume old session"})
    sessions = store.list()

    assert sessions[0]["session_id"] == first.session_id
    assert sessions[1]["session_id"] == second.session_id


def test_session_store_last_survives_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo", language="zh-CN")

    reloaded = SessionStore(db_path)

    assert reloaded.last().session_id == session.session_id


def test_session_store_last_tracks_recent_activity_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    first = store.create(workspace="/repo1", language="zh-CN")
    second = store.create(workspace="/repo2", language="zh-CN")

    store.append_message(first, {"message": "resume old session"})
    reloaded = SessionStore(db_path)

    assert reloaded.last().session_id == first.session_id
    assert reloaded.list()[1]["session_id"] == second.session_id


def test_session_store_migrates_old_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            create table sessions (
                session_id text primary key,
                workspace text not null,
                language text not null,
                created_at text not null
            );
            create table messages (
                id integer primary key autoincrement,
                session_id text not null,
                role text not null,
                payload text not null,
                created_at text not null
            );
            insert into sessions (session_id, workspace, language, created_at)
            values ('sess_old', '/repo', 'zh-CN', '2026-07-16T00:00:00+00:00');
            """
        )

    store = SessionStore(db_path)
    session = store.get("sess_old")

    assert session is not None
    assert session.updated_at.isoformat() == "2026-07-16T00:00:00+00:00"


@pytest.mark.asyncio
async def test_session_events_assign_ids_and_support_independent_subscribers() -> None:
    events = SessionEvents()
    await events.put({"type": "one"})

    first = events.subscribe(after=0)
    second = events.subscribe(after=0)

    first_event = await first.__anext__()
    second_event = await second.__anext__()

    assert first_event == second_event
    assert first_event["event_id"] == 1
    assert events.last_event_id() == 1

    await first.aclose()
    await second.aclose()


@pytest.mark.asyncio
async def test_session_events_default_after_marks_latest_run_start() -> None:
    events = SessionEvents()
    await events.put({"type": "final"})
    events.set_default_after(events.last_event_id())
    await events.put({"type": "plan.created"})

    replay = events.events_after(events.default_after())

    assert [event["type"] for event in replay] == ["plan.created"]


@pytest.mark.asyncio
async def test_session_store_persists_events(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo", language="zh-CN")

    await session.events.put({"type": "plan.created"})
    await session.events.put({"type": "final", "summary": "done"})

    reloaded = SessionStore(db_path)
    restored = reloaded.get(session.session_id)

    assert restored is not None
    events = restored.events.events_after(0)
    assert [event["type"] for event in events] == ["plan.created", "final"]
    assert [event["event_id"] for event in events] == [1, 2]


@pytest.mark.asyncio
async def test_session_events_trim_retained_events() -> None:
    events = SessionEvents(max_events=3)
    for index in range(5):
        await events.put({"type": f"event.{index}"})

    retained = events.events_after(0)

    assert [event["event_id"] for event in retained] == [3, 4, 5]
    assert events.retained_count() == 3
    assert events.last_event_id() == 5


@pytest.mark.asyncio
async def test_assistant_delta_is_not_persisted() -> None:
    persisted: list[dict] = []
    events = SessionEvents(on_event=persisted.append)

    await events.put({"type": "assistant.delta", "text": "他"})
    await events.put({"type": "final", "summary": "done"})

    assert [call["type"] for call in persisted] == ["final"]


@pytest.mark.asyncio
async def test_assistant_delta_still_delivered_to_live_subscribers() -> None:
    events = SessionEvents()

    await events.put({"type": "assistant.delta", "text": "你"})
    await events.put({"type": "assistant.delta", "text": "好"})

    retained = events.events_after(0)

    assert [event.get("text") for event in retained] == ["你", "好"]


@pytest.mark.asyncio
async def test_assistant_delta_flood_does_not_evict_real_events() -> None:
    events = SessionEvents(max_events=250)
    await events.put({"type": "tool.started", "tool": "read_file"})

    for _ in range(300):
        await events.put({"type": "assistant.delta", "text": "x"})

    retained = events.events_after(0)
    types = [event["type"] for event in retained]

    assert "tool.started" in types
    assert types.count("assistant.delta") <= 200


@pytest.mark.asyncio
async def test_session_store_prunes_persisted_events(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path, event_limit=2)
    session = store.create(workspace="/repo", language="zh-CN")

    for index in range(4):
        await session.events.put({"type": f"event.{index}"})

    with sqlite3.connect(db_path) as conn:
        count = conn.execute("select count(*) from events where session_id = ?", (session.session_id,)).fetchone()[0]

    reloaded = SessionStore(db_path, event_limit=2)
    restored = reloaded.get(session.session_id)

    assert count == 2
    assert restored is not None
    assert [event["event_id"] for event in restored.events.events_after(0)] == [3, 4]


def test_normalize_event_limit_uses_minimum_one(monkeypatch: pytest.MonkeyPatch) -> None:
    assert normalize_event_limit(0) == 1

    monkeypatch.setenv("AICODE_SESSION_EVENT_LIMIT", "bad")
    assert normalize_event_limit() == 2_000
