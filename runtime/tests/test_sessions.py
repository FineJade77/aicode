import sqlite3
import threading
from pathlib import Path

import pytest

from app.agent.session import CompactionEntry
from app.sessions.store import (
    COMPACTION_SCHEMA_VERSION,
    MIGRATIONS,
    SCHEMA_VERSION,
    SchemaVersionError,
    Session,
    SessionEvents,
    SessionStore,
    normalize_cache_limit,
    normalize_event_limit,
)


def test_session_store_persists_sessions_and_messages(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo")
    store.append_message(session, {"message": "hello", "mode": "chat"})

    reloaded = SessionStore(db_path)
    restored = reloaded.get(session.session_id)

    assert restored is not None
    assert restored.session_id == session.session_id
    assert restored.workspace == "/repo"
    assert restored.messages == [{"message": "hello", "mode": "chat"}]


def test_session_store_lists_newest_first(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    first = store.create(workspace="/repo1")
    second = store.create(workspace="/repo2")

    sessions = store.list()

    assert sessions[0]["session_id"] == second.session_id
    assert sessions[1]["session_id"] == first.session_id


def test_session_store_lists_recently_updated_first(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    first = store.create(workspace="/repo1")
    second = store.create(workspace="/repo2")

    store.append_message(first, {"message": "resume old session"})
    sessions = store.list()

    assert sessions[0]["session_id"] == first.session_id
    assert sessions[1]["session_id"] == second.session_id


def test_session_store_last_survives_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo")

    reloaded = SessionStore(db_path)

    assert reloaded.last().session_id == session.session_id


def test_session_store_last_tracks_recent_activity_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    first = store.create(workspace="/repo1")
    second = store.create(workspace="/repo2")

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
            values ('sess_old', '/repo', 'en-US', '2026-07-16T00:00:00+00:00');
            """
        )

    store = SessionStore(db_path)
    session = store.get("sess_old")

    assert session is not None
    assert session.updated_at.isoformat() == "2026-07-16T00:00:00+00:00"
    assert session.compactions == []
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(sessions)").fetchall()}
        assert columns == {"session_id", "workspace", "created_at", "updated_at", "plan"}
        assert conn.execute(
            "select count(*) from sqlite_master where type = 'table' and name = 'compactions'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_session_events_assign_ids_and_support_independent_subscribers() -> None:
    events = SessionEvents()
    await events.put({"type": "run.started"})

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
async def test_session_events_redact_known_runtime_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret-value")
    events = SessionEvents()

    await events.put({"type": "tool.output", "tool": "bash", "text": "provider-secret-value"})

    event = events.events_after(0)[0]
    assert "provider-secret-value" not in event["text"]
    assert event["text"] == "[REDACTED]"

    restored = SessionEvents(
        events=[{"type": "tool.output", "tool": "bash", "text": "provider-secret-value", "event_id": 1}]
    )
    assert restored.events_after(0)[0]["text"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_session_events_default_after_marks_latest_run_start() -> None:
    events = SessionEvents()
    await events.put({"type": "final"})
    events.set_default_after(events.last_event_id())
    await events.put({"type": "run.started"})

    replay = events.events_after(events.default_after())

    assert [event["type"] for event in replay] == ["run.started"]


@pytest.mark.asyncio
async def test_session_store_persists_events(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo")

    await session.events.put({"type": "run.started"})
    await session.events.put({"type": "final", "summary": "done"})
    await store.flush()

    reloaded = SessionStore(db_path)
    restored = reloaded.get(session.session_id)

    assert restored is not None
    events = restored.events.events_after(0)
    assert [event["type"] for event in events] == ["run.started", "final"]
    assert [event["event_id"] for event in events] == [1, 2]


@pytest.mark.asyncio
async def test_session_store_expires_unresolved_approval_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
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
    await store.flush()

    reloaded = SessionStore(db_path)
    restored = reloaded.get(session.session_id)

    assert restored is not None
    events = restored.events.events_after(0)
    assert [event["type"] for event in events] == ["approval.requested", "approval.expired", "tool.rejected"]
    assert events[1]["approval_id"] == "appr_1"
    assert events[1]["tool_call_id"] == "tc_1"
    assert events[2]["tool_call_id"] == "tc_1"

    reloaded_again = SessionStore(db_path)
    restored_again = reloaded_again.get(session.session_id)
    assert restored_again is not None
    assert [event["type"] for event in restored_again.events.events_after(0)].count("approval.expired") == 1


@pytest.mark.asyncio
async def test_session_store_does_not_expire_resolved_approval_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
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
    await session.events.put({"type": "tool.output", "tool": "bash", "tool_call_id": "tc_1", "text": "ok"})
    await store.flush()

    reloaded = SessionStore(db_path)
    restored = reloaded.get(session.session_id)

    assert restored is not None
    assert [event["type"] for event in restored.events.events_after(0)] == ["approval.requested", "tool.output"]


@pytest.mark.asyncio
async def test_session_store_expires_unresolved_edit_approval_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo")

    await session.events.put(
        {
            "type": "approval.requested",
            "approval_id": "appr_1",
            "kind": "edit",
            "path": "a.py",
            "tool_call_id": "tc_edit",
            "message": "Waiting for approval",
        }
    )
    await store.flush()

    reloaded = SessionStore(db_path)
    restored = reloaded.get(session.session_id)

    assert restored is not None
    events = restored.events.events_after(0)
    assert [event["type"] for event in events] == ["approval.requested", "approval.expired", "edit.rejected"]
    assert events[2]["path"] == "a.py"
    assert events[2]["tool_call_id"] == "tc_edit"


@pytest.mark.asyncio
async def test_session_events_trim_retained_events() -> None:
    events = SessionEvents(max_events=3)
    for index in range(5):
        await events.put({"type": "final", "summary": str(index)})

    retained = events.events_after(0)

    assert [event["event_id"] for event in retained] == [3, 4, 5]
    assert events.retained_count() == 3
    assert events.last_event_id() == 5


@pytest.mark.asyncio
async def test_assistant_delta_is_not_persisted() -> None:
    persisted: list[dict] = []
    events = SessionEvents(on_event=persisted.append)

    await events.put({"type": "assistant.delta", "text": "old"})
    await events.put({"type": "final", "summary": "done"})

    assert [call["type"] for call in persisted] == ["final"]


@pytest.mark.asyncio
async def test_assistant_delta_still_delivered_to_live_subscribers() -> None:
    events = SessionEvents()

    await events.put({"type": "assistant.delta", "text": "hel"})
    await events.put({"type": "assistant.delta", "text": "lo"})

    retained = events.events_after(0)

    assert [event.get("text") for event in retained] == ["hel", "lo"]


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
    session = store.create(workspace="/repo")

    for index in range(4):
        await session.events.put({"type": "final", "summary": str(index)})
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
    session = store.create(workspace="/repo")

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
    session = store.create(workspace="/repo")

    original_write = store._write_event_sync
    call_count = {"value": 0}

    def flaky_write(session_id: str, event: dict) -> None:
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise RuntimeError("simulated disk error")
        original_write(session_id, event)

    monkeypatch.setattr(store, "_write_event_sync", flaky_write)

    await session.events.put({"type": "run.started"})
    await session.events.put({"type": "final"})
    await store.flush()
    status = store.event_writer_status()

    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "select count(*) from events where session_id = ?", (session.session_id,)
        ).fetchone()[0]

    # The first failed write is swallowed without stopping the loop; the second persists.
    assert count == 1
    assert status["enqueued"] == 2
    assert status["written"] == 1
    assert status["failed"] == 1


@pytest.mark.asyncio
async def test_flush_is_a_noop_when_nothing_was_ever_written(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    # No event was put, so the writer never started; flush must neither fail nor hang.
    await store.flush()


@pytest.mark.asyncio
async def test_aclose_flushes_and_stops_event_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo")

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
    first = store.create(workspace="/repo1")
    second = store.create(workspace="/repo2")
    third = store.create(workspace="/repo3")

    assert len(store._sessions) == 2
    assert first.session_id not in store._sessions
    assert second.session_id in store._sessions
    assert third.session_id in store._sessions


def test_evicted_session_is_still_reachable_via_get(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=1)
    first = store.create(workspace="/repo1")
    store.create(workspace="/repo2")

    assert first.session_id not in store._sessions

    reloaded = store.get(first.session_id)

    assert reloaded is not None
    assert reloaded.session_id == first.session_id
    assert reloaded.workspace == "/repo1"


def test_get_refreshes_recency_and_protects_from_eviction(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=2)
    first = store.create(workspace="/repo1")
    store.create(workspace="/repo2")

    # Touch first so it becomes the most recently used session.
    store.get(first.session_id)

    store.create(workspace="/repo3")

    # first was just accessed and must remain; repo2 should be evicted.
    assert first.session_id in store._sessions


def test_session_with_pending_approval_is_never_evicted(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=1)
    pending = store.create(workspace="/repo1")
    pending.create_approval("edit", {"path": "a.py"})

    idle = store.create(workspace="/repo2")

    # With cache_limit=1, pending cannot be evicted due to its approval; idle is the only eligible session.
    assert pending.session_id in store._sessions
    assert idle.session_id not in store._sessions


def test_session_with_active_agent_runner_is_never_evicted(tmp_path: Path) -> None:
    import asyncio

    async def _never_finishes() -> None:
        await asyncio.sleep(3600)

    async def run() -> None:
        store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=1)
        running = store.create(workspace="/repo1")
        running.agent_runner_task = asyncio.create_task(_never_finishes())

        idle = store.create(workspace="/repo2")

        assert running.session_id in store._sessions
        assert idle.session_id not in store._sessions

        running.agent_runner_task.cancel()
        try:
            await running.agent_runner_task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_session_status_exposes_active_run_progress() -> None:
    session = Session(session_id="sess_test", workspace="/repo")

    session.start_agent_run("run_test")
    session.mark_agent_progress("tool.bash")
    agent = session.to_dict()["agent"]

    assert agent["current_run_id"] == "run_test"
    assert agent["stage"] == "tool.bash"
    assert agent["started_at"] is not None
    assert agent["last_progress_at"] is not None
    assert agent["elapsed_seconds"] >= 0
    assert agent["stalled_seconds"] >= 0


def test_normalize_cache_limit_uses_minimum_one(monkeypatch: pytest.MonkeyPatch) -> None:
    assert normalize_cache_limit(0) == 1

    monkeypatch.setenv("AICODE_SESSION_CACHE_LIMIT", "bad")
    assert normalize_cache_limit() == 200


def test_session_store_uses_wal_and_a_bounded_lock_wait(tmp_path: Path) -> None:
    """WAL lets list()/resume read while a run is still appending messages, and
    busy_timeout makes a contended write wait instead of failing immediately."""
    store = SessionStore(path=tmp_path / "s.sqlite")
    store.create(workspace=str(tmp_path))

    with store._connect() as conn:
        assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
        assert conn.execute("pragma synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("pragma busy_timeout").fetchone()[0] == 5000


def test_session_store_reuses_one_connection(tmp_path: Path) -> None:
    """Opening a connection per call cost roughly 5x the write itself."""
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path))

    with store._connect() as first:
        pass
    store.append_message(session, {"role": "user", "content": "hi"})
    with store._connect() as second:
        pass

    assert first is second


def test_concurrent_writes_and_reads_do_not_lock(tmp_path: Path) -> None:
    """The shared connection is used from both the event loop and the
    write-behind worker thread, so contention must be serialized rather than
    surfacing as `database is locked`."""
    store = SessionStore(path=tmp_path / "s.sqlite")
    sessions = [store.create(workspace=str(tmp_path)) for _ in range(4)]
    errors: list[Exception] = []

    def writer(session) -> None:
        try:
            for index in range(25):
                store.append_message(session, {"role": "user", "content": f"m{index}"})
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(25):
                store.list()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(session,)) for session in sessions]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    for session in sessions:
        assert len(store.get(session.session_id).messages) == 25


@pytest.mark.asyncio
async def test_session_store_aclose_releases_the_connection(tmp_path: Path) -> None:
    store = SessionStore(path=tmp_path / "s.sqlite")
    store.create(workspace=str(tmp_path))
    with store._connect():
        pass
    assert store._conn is not None

    await store.aclose()

    assert store._conn is None


def test_fresh_database_lands_on_the_current_schema_version(tmp_path: Path) -> None:
    store = SessionStore(path=tmp_path / "s.sqlite")
    store.create(workspace=str(tmp_path))

    with store._connect() as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION


def test_legacy_database_without_user_version_is_migrated_in_place(tmp_path: Path) -> None:
    """Databases created before the ladder carry user_version=0 while already
    holding the tables, and must be advanced without losing rows."""
    db = tmp_path / "s.sqlite"
    legacy = sqlite3.connect(db)
    legacy.executescript(
        """
        create table sessions (
            session_id text primary key,
            workspace text not null,
            created_at text not null,
            language text
        );
        create table messages (
            id integer primary key autoincrement,
            session_id text not null,
            role text not null,
            payload text not null,
            created_at text not null
        );
        insert into sessions (session_id, workspace, created_at, language)
        values ('sess_old', '/repo', '2026-01-01T00:00:00+00:00', 'zh');
        insert into messages (session_id, role, payload, created_at)
        values ('sess_old', 'user', '{"role":"user","content":"legacy"}', '2026-01-01T00:00:00+00:00');
        """
    )
    legacy.commit()
    legacy.close()

    store = SessionStore(path=db)
    session = store.get("sess_old")

    assert session is not None
    assert session.workspace == "/repo"
    assert [message["content"] for message in session.messages] == ["legacy"]
    with store._connect() as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row["name"] for row in conn.execute("pragma table_info(sessions)").fetchall()}
        assert "updated_at" in columns
        assert "language" not in columns


def test_database_from_a_newer_build_is_refused(tmp_path: Path) -> None:
    """Continuing against an unknown schema risks writing rows an older build
    cannot read back, so refusing is the correct behaviour."""
    db = tmp_path / "s.sqlite"
    seeded = SessionStore(path=db)
    seeded.create(workspace=str(tmp_path))
    seeded._close_connection()
    future = sqlite3.connect(db)
    future.execute(f"pragma user_version = {SCHEMA_VERSION + 1}")
    future.commit()
    future.close()

    store = SessionStore(path=db)
    with pytest.raises(SchemaVersionError) as excinfo:
        store.create(workspace=str(tmp_path))

    message = str(excinfo.value)
    assert str(SCHEMA_VERSION + 1) in message
    assert "Upgrade aicode" in message, "the refusal must tell the user what to do"


def test_migrations_are_append_only_and_ordered(tmp_path: Path) -> None:
    """Renumbering or reordering a shipped migration would desynchronise every
    database in the field, which records how far it has been advanced."""
    versions = [version for version, _ in MIGRATIONS]
    assert versions == sorted(versions)
    assert versions == list(range(1, len(versions) + 1))
    assert SCHEMA_VERSION == versions[-1]


def test_already_current_but_unversioned_database_is_stamped_without_changes(tmp_path: Path) -> None:
    """The real upgrade path for existing users.

    A database written by the build just before the ladder already has the final
    table shape but carries user_version=0. Every migration must be a no-op and
    the data must be untouched — only the version stamp advances.
    """
    db = tmp_path / "s.sqlite"
    seeded = SessionStore(path=db)
    session = seeded.create(workspace=str(tmp_path))
    seeded.append_message(session, {"role": "user", "content": "keep me"})
    seeded._close_connection()
    # Simulate the pre-ladder state: correct schema, no version recorded.
    unversioned = sqlite3.connect(db)
    unversioned.execute("pragma user_version = 0")
    unversioned.commit()
    unversioned.close()

    store = SessionStore(path=db)
    restored = store.get(session.session_id)

    assert restored is not None
    assert [message["content"] for message in restored.messages] == ["keep me"]
    with store._connect() as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("select count(*) from messages").fetchone()[0] == 1


def test_list_returns_summaries_without_hydrating_history(tmp_path: Path) -> None:
    """The listing must not carry every message of every session.

    The previous implementation hydrated all history on each call, which grew
    without bound even though the only consumer shows an overview.
    """
    store = SessionStore(path=tmp_path / "s.sqlite")
    session = store.create(workspace=str(tmp_path))
    for index in range(5):
        store.append_message(session, {"role": "user", "content": f"m{index}"})

    rows = store.list()

    assert len(rows) == 1
    assert "messages" not in rows[0]
    assert rows[0]["message_count"] == 5
    assert rows[0]["agent"]["running"] is False


def test_list_does_not_populate_the_session_cache(tmp_path: Path) -> None:
    """Listing is a read-only overview and must not evict live sessions."""
    db = tmp_path / "s.sqlite"
    seeded = SessionStore(path=db)
    seeded.create(workspace=str(tmp_path))
    seeded._close_connection()

    store = SessionStore(path=db)
    store.list()

    assert store._sessions == {}


def test_list_supports_limit_and_offset(tmp_path: Path) -> None:
    store = SessionStore(path=tmp_path / "s.sqlite")
    created = [store.create(workspace=f"/repo{index}") for index in range(5)]
    newest_first = [session.session_id for session in reversed(created)]

    assert [row["session_id"] for row in store.list(limit=2)] == newest_first[:2]
    assert [row["session_id"] for row in store.list(limit=2, offset=2)] == newest_first[2:4]
    assert [row["session_id"] for row in store.list(offset=4)] == newest_first[4:]


def test_prune_is_disabled_unless_a_bound_is_given(tmp_path: Path) -> None:
    """Silently deleting conversation history is worse than an unbounded database."""
    store = SessionStore(path=tmp_path / "s.sqlite")
    store.create(workspace=str(tmp_path))

    result = store.prune()

    assert result["status"] == "disabled"
    assert result["deleted_sessions"] == 0
    assert len(store.list()) == 1


def test_prune_by_max_sessions_keeps_the_newest(tmp_path: Path) -> None:
    store = SessionStore(path=tmp_path / "s.sqlite")
    created = [store.create(workspace=f"/repo{index}") for index in range(5)]
    for session in created:
        store.append_message(session, {"role": "user", "content": "x"})
    store._sessions.clear()

    result = store.prune(max_sessions=2)

    assert result["deleted_sessions"] == 3
    assert result["deleted_messages"] == 3
    remaining = {row["session_id"] for row in store.list()}
    assert remaining == {created[-1].session_id, created[-2].session_id}


def test_prune_removes_messages_events_and_compactions(tmp_path: Path) -> None:
    store = SessionStore(path=tmp_path / "s.sqlite")
    doomed = store.create(workspace="/old")
    store.append_message(doomed, {"role": "user", "content": "x"})
    store.append_compaction(
        doomed,
        CompactionEntry(
            compaction_id=None,
            session_id=doomed.session_id,
            schema_version=COMPACTION_SCHEMA_VERSION,
            start_message_id=1,
            end_message_id=1,
            summary="- old",
            provider="fake",
            model="fake",
            prompt_version="v1",
            before_tokens=10,
            after_tokens=5,
            context_window=1000,
        ),
    )
    store.create(workspace="/new")
    store._sessions.clear()

    store.prune(max_sessions=1)

    with store._connect() as conn:
        for table in ("messages", "events", "compactions"):
            leftover = conn.execute(
                f"select count(*) from {table} where session_id = ?", (doomed.session_id,)
            ).fetchone()[0]
            assert leftover == 0, f"{table} rows outlived their session"


def test_prune_never_deletes_a_session_with_a_pending_approval(tmp_path: Path) -> None:
    """An unresolved approval means state is about to be written back."""
    store = SessionStore(path=tmp_path / "s.sqlite")
    live = store.create(workspace="/live")
    live.create_approval("edit", {"path": "a.py"})
    store.create(workspace="/newer")

    result = store.prune(max_sessions=1)

    assert result["deleted_sessions"] == 0
    assert result["retained_live"] == 1
    assert store.get(live.session_id) is not None


def test_prune_by_max_age_uses_updated_at(tmp_path: Path) -> None:
    store = SessionStore(path=tmp_path / "s.sqlite")
    old = store.create(workspace="/old")
    recent = store.create(workspace="/recent")
    store._sessions.clear()
    with store._connect() as conn:
        conn.execute(
            "update sessions set updated_at = ? where session_id = ?",
            ("2020-01-01T00:00:00+00:00", old.session_id),
        )

    result = store.prune(max_age_days=30)

    assert result["deleted_sessions"] == 1
    assert [row["session_id"] for row in store.list()] == [recent.session_id]


def test_a_v4_database_gains_the_structured_column_without_losing_compactions(tmp_path: Path) -> None:
    """The pre-T-039 shape must upgrade in place.

    The compaction *schema version* bump makes old entries ignored for
    projection, but the rows themselves are still history and must survive the
    SQL migration.
    """
    db = tmp_path / "s.sqlite"
    older = sqlite3.connect(db)
    older.executescript(
        """
        create table sessions (
            session_id text primary key,
            workspace text not null,
            created_at text not null,
            updated_at text not null,
            plan text
        );
        create table messages (
            id integer primary key autoincrement,
            session_id text not null,
            role text not null,
            payload text not null,
            created_at text not null
        );
        create table compactions (
            id integer primary key autoincrement,
            session_id text not null,
            schema_version integer not null,
            start_message_id integer not null,
            end_message_id integer not null,
            summary text not null,
            provider text not null,
            model text not null,
            prompt_version text not null,
            before_tokens integer not null,
            after_tokens integer not null,
            context_window integer not null,
            created_at text not null
        );
        create table events (
            id integer primary key autoincrement,
            session_id text not null,
            sequence integer not null,
            payload text not null,
            created_at text not null
        );
        insert into sessions (session_id, workspace, created_at, updated_at, plan)
        values ('sess_v4', '/repo', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', null);
        insert into messages (session_id, role, payload, created_at)
        values ('sess_v4', 'user', '{"role":"user","content":"hello"}', '2026-01-01T00:00:00+00:00');
        insert into compactions (
            session_id, schema_version, start_message_id, end_message_id, summary,
            provider, model, prompt_version, before_tokens, after_tokens, context_window, created_at
        ) values ('sess_v4', 1, 1, 1, '- free text summary', 'openai', 'gpt', 'aicode.compaction.v1',
                  100, 50, 32768, '2026-01-01T00:00:00+00:00');
        """
    )
    older.execute("pragma user_version = 4")
    older.commit()
    older.close()

    store = SessionStore(path=db)
    session = store.get("sess_v4")

    assert session is not None
    assert [message["content"] for message in session.messages] == ["hello"]
    assert len(session.compactions) == 1
    legacy = session.compactions[0]
    assert legacy.summary == "- free text summary"
    assert legacy.structured is None, "a v1 row has no structure to load"
    with store._connect() as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row["name"] for row in conn.execute("pragma table_info(compactions)").fetchall()}
        assert "structured" in columns


def seeded_session(store: SessionStore, count: int = 4):
    session = store.create(workspace="/repo")
    for index in range(count):
        store.append_message(session, {"role": "user", "content": f"m{index}"})
    return session


def test_fork_copies_history_up_to_the_chosen_message(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s.sqlite")
    source = seeded_session(store)

    forked = store.fork(source.session_id, message_id=source.message_ids[1])

    assert [m["content"] for m in forked.messages] == ["m0", "m1"]
    assert forked.workspace == source.workspace
    assert forked.session_id != source.session_id


def test_fork_without_a_message_id_branches_at_the_tip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s.sqlite")
    source = seeded_session(store)

    forked = store.fork(source.session_id)

    assert [m["content"] for m in forked.messages] == ["m0", "m1", "m2", "m3"]


def test_forked_history_is_copied_not_shared(tmp_path: Path) -> None:
    """Sharing rows would make each session's future extend the other's past."""
    store = SessionStore(tmp_path / "s.sqlite")
    source = seeded_session(store, count=2)
    forked = store.fork(source.session_id)

    store.append_message(forked, {"role": "user", "content": "only-in-fork"})
    store.append_message(source, {"role": "user", "content": "only-in-source"})

    store._sessions.clear()
    assert [m["content"] for m in store.get(source.session_id).messages] == ["m0", "m1", "only-in-source"]
    assert [m["content"] for m in store.get(forked.session_id).messages] == ["m0", "m1", "only-in-fork"]


def test_fork_survives_a_reload(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s.sqlite")
    source = seeded_session(store)
    forked = store.fork(source.session_id, message_id=source.message_ids[2])

    store._sessions.clear()
    reloaded = store.get(forked.session_id)

    assert [m["content"] for m in reloaded.messages] == ["m0", "m1", "m2"]


def test_fork_rejects_a_message_from_another_session(tmp_path: Path) -> None:
    """Clamping would produce a session that looks right and holds wrong history."""
    store = SessionStore(tmp_path / "s.sqlite")
    source = seeded_session(store)
    other = seeded_session(store)

    with pytest.raises(ValueError, match="does not belong"):
        store.fork(source.session_id, message_id=other.message_ids[0])


def test_fork_of_a_missing_session_is_an_error(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s.sqlite")
    with pytest.raises(ValueError, match="session not found"):
        store.fork("sess_nope")


def test_fork_remaps_compaction_bounds_onto_the_new_messages(tmp_path: Path) -> None:
    """Message ids are global, so an unremapped bound points at other rows.

    Carrying the entry over verbatim would leave the fork's projection
    summarising messages that belong to a different session.
    """
    store = SessionStore(tmp_path / "s.sqlite")
    source = seeded_session(store)
    store.append_compaction(
        source,
        CompactionEntry(
            compaction_id=None,
            session_id=source.session_id,
            schema_version=COMPACTION_SCHEMA_VERSION,
            start_message_id=source.message_ids[0],
            end_message_id=source.message_ids[1],
            summary="- earlier work",
            provider="fake",
            model="fake",
            prompt_version="v1",
            before_tokens=10,
            after_tokens=5,
            context_window=1000,
        ),
    )

    forked = store.fork(source.session_id)

    (entry,) = forked.compactions
    assert entry.session_id == forked.session_id
    assert entry.start_message_id == forked.message_ids[0]
    assert entry.end_message_id == forked.message_ids[1]
    assert entry.summary == "- earlier work"


def test_a_compaction_past_the_fork_point_is_left_behind(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s.sqlite")
    source = seeded_session(store)
    store.append_compaction(
        source,
        CompactionEntry(
            compaction_id=None,
            session_id=source.session_id,
            schema_version=COMPACTION_SCHEMA_VERSION,
            start_message_id=source.message_ids[2],
            end_message_id=source.message_ids[3],
            summary="- later work",
            provider="fake",
            model="fake",
            prompt_version="v1",
            before_tokens=10,
            after_tokens=5,
            context_window=1000,
        ),
    )

    forked = store.fork(source.session_id, message_id=source.message_ids[1])

    assert forked.compactions == []


def test_fork_carries_the_plan_but_not_the_read_record(tmp_path: Path) -> None:
    """`read_files` is in-memory by design: the guarantee is "seen in *this*
    conversation", so a fork must re-read rather than inherit the claim."""
    from app.sessions.store import PlanItem

    store = SessionStore(tmp_path / "s.sqlite")
    source = seeded_session(store)
    source.set_plan([PlanItem(text="ship it", status="pending")])
    source.record_read("a.py", "hash-a")

    forked = store.fork(source.session_id)

    assert [item.text for item in forked.plan] == ["ship it"]
    assert forked.read_hash("a.py") is None
