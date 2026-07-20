import sqlite3
from pathlib import Path

import pytest

from app.sessions.store import SessionEvents, SessionStore, normalize_cache_limit, normalize_event_limit


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
    await store.flush()

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
    await store.flush()

    with sqlite3.connect(db_path) as conn:
        count = conn.execute("select count(*) from events where session_id = ?", (session.session_id,)).fetchone()[0]

    reloaded = SessionStore(db_path, event_limit=2)
    restored = reloaded.get(session.session_id)

    assert count == 2
    assert restored is not None
    assert [event["event_id"] for event in restored.events.events_after(0)] == [3, 4]


@pytest.mark.asyncio
async def test_flush_waits_for_pending_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo", language="zh-CN")

    await session.events.put({"type": "tool.started", "tool": "read_file"})
    await store.flush()
    status = store.event_writer_status()

    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "select count(*) from events where session_id = ?", (session.session_id,)
        ).fetchone()[0]

    assert count == 1
    assert status["enqueued"] == 1
    assert status["written"] == 1
    assert status["dropped"] == 0
    assert status["failed"] == 0


@pytest.mark.asyncio
async def test_event_writer_survives_individual_write_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo", language="zh-CN")

    original_write = store._write_event_sync
    call_count = {"value": 0}

    def flaky_write(session_id: str, event: dict) -> None:
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise RuntimeError("simulated disk error")
        original_write(session_id, event)

    monkeypatch.setattr(store, "_write_event_sync", flaky_write)

    await session.events.put({"type": "one"})
    await session.events.put({"type": "two"})
    await store.flush()
    status = store.event_writer_status()

    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "select count(*) from events where session_id = ?", (session.session_id,)
        ).fetchone()[0]

    # 第一条写入失败被吞掉（不中断写入循环），第二条成功落盘
    assert count == 1
    assert status["enqueued"] == 2
    assert status["written"] == 1
    assert status["failed"] == 1


@pytest.mark.asyncio
async def test_flush_is_a_noop_when_nothing_was_ever_written(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    # 从未 put 过事件，writer 从未启动；flush 不应抛错或挂起
    await store.flush()


@pytest.mark.asyncio
async def test_aclose_flushes_and_stops_event_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo", language="zh-CN")

    await session.events.put({"type": "final", "summary": "done"})
    assert store.event_writer_status()["writer_running"] is True

    await store.aclose()
    status = store.event_writer_status()

    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "select count(*) from events where session_id = ?", (session.session_id,)
        ).fetchone()[0]

    assert count == 1
    assert status["writer_running"] is False
    assert status["queue_size"] == 0


def test_normalize_event_limit_uses_minimum_one(monkeypatch: pytest.MonkeyPatch) -> None:
    assert normalize_event_limit(0) == 1

    monkeypatch.setenv("AICODE_SESSION_EVENT_LIMIT", "bad")
    assert normalize_event_limit() == 2_000


def test_idle_sessions_are_evicted_beyond_cache_limit(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=2)
    first = store.create(workspace="/repo1", language="zh-CN")
    second = store.create(workspace="/repo2", language="zh-CN")
    third = store.create(workspace="/repo3", language="zh-CN")

    assert len(store._sessions) == 2
    assert first.session_id not in store._sessions
    assert second.session_id in store._sessions
    assert third.session_id in store._sessions


def test_evicted_session_is_still_reachable_via_get(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=1)
    first = store.create(workspace="/repo1", language="zh-CN")
    store.create(workspace="/repo2", language="zh-CN")

    assert first.session_id not in store._sessions

    reloaded = store.get(first.session_id)

    assert reloaded is not None
    assert reloaded.session_id == first.session_id
    assert reloaded.workspace == "/repo1"


def test_get_refreshes_recency_and_protects_from_eviction(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=2)
    first = store.create(workspace="/repo1", language="zh-CN")
    store.create(workspace="/repo2", language="zh-CN")

    # 触碰 first，使其成为最近使用
    store.get(first.session_id)

    store.create(workspace="/repo3", language="zh-CN")

    # first 因为刚被访问过，不应被驱逐；repo2 应被驱逐
    assert first.session_id in store._sessions


def test_session_with_pending_approval_is_never_evicted(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=1)
    pending = store.create(workspace="/repo1", language="zh-CN")
    pending.create_approval("edit", {"path": "a.py"})

    idle = store.create(workspace="/repo2", language="zh-CN")

    # cache_limit=1 且 pending 有未决 approval，不可驱逐；idle 是唯一可驱逐的，被驱逐出去
    assert pending.session_id in store._sessions
    assert idle.session_id not in store._sessions


def test_session_with_active_agent_runner_is_never_evicted(tmp_path: Path) -> None:
    import asyncio

    async def _never_finishes() -> None:
        await asyncio.sleep(3600)

    async def run() -> None:
        store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=1)
        running = store.create(workspace="/repo1", language="zh-CN")
        running.agent_runner_task = asyncio.create_task(_never_finishes())

        idle = store.create(workspace="/repo2", language="zh-CN")

        assert running.session_id in store._sessions
        assert idle.session_id not in store._sessions

        running.agent_runner_task.cancel()
        try:
            await running.agent_runner_task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_normalize_cache_limit_uses_minimum_one(monkeypatch: pytest.MonkeyPatch) -> None:
    assert normalize_cache_limit(0) == 1

    monkeypatch.setenv("AICODE_SESSION_CACHE_LIMIT", "bad")
    assert normalize_cache_limit() == 200
