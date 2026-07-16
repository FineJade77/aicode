import sqlite3
from pathlib import Path

from app.sessions.store import SessionStore


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
