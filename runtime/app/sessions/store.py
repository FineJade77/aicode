from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


DEFAULT_SESSION_EVENT_LIMIT = 2_000
MAX_TRANSIENT_RETAINED_EVENTS = 200
EVENT_WRITE_QUEUE_MAXSIZE = 5_000
DEFAULT_SESSION_CACHE_LIMIT = 200


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    kind: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decision_event: asyncio.Event = field(default_factory=asyncio.Event)
    accepted: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "kind": self.kind,
            "status": "pending" if self.accepted is None else ("accepted" if self.accepted else "rejected"),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(slots=True)
class QueuedAgentRun:
    run_id: str
    request: Any


class SessionEvents:
    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        max_events: int | None = None,
    ) -> None:
        self._events = [dict(event) for event in events or []]
        self._condition = asyncio.Condition()
        self._on_event = on_event
        self._read_after = 0
        self._next_sequence = max((int(event.get("event_id") or 0) for event in self._events), default=0) + 1
        self._default_after: int | None = None
        self._default_run_id: str | None = None
        self._current_run_id: str | None = None
        self._max_events = normalize_event_limit(max_events)
        self._trim_retained_events()

    async def put(self, event: dict[str, Any]) -> None:
        event = dict(event)
        if self._current_run_id and "run_id" not in event:
            event["run_id"] = self._current_run_id
        if "event_id" not in event:
            event["event_id"] = self._next_sequence
            self._next_sequence += 1
        else:
            self._next_sequence = max(self._next_sequence, int(event["event_id"]) + 1)

        if self._on_event is not None and event.get("type") != "assistant.delta":
            try:
                self._on_event(event)
            except Exception:
                pass

        async with self._condition:
            self._events.append(event)
            self._trim_retained_events()
            self._condition.notify_all()

    async def get(self) -> dict[str, Any]:
        async with self._condition:
            while True:
                event = self._first_event_after(self._read_after)
                if event is not None:
                    self._read_after = int(event.get("event_id") or self._read_after)
                    return dict(event)
                await self._condition.wait()

    def empty(self) -> bool:
        return self._first_event_after(self._read_after) is None

    def events_after(self, after: int | None = None) -> list[dict[str, Any]]:
        after = after or 0
        return [dict(event) for event in self._events if int(event.get("event_id") or 0) > after]

    def last_event_id(self) -> int:
        return self._next_sequence - 1

    def set_default_after(self, after: int | None) -> None:
        self._default_after = after

    def default_after(self) -> int | None:
        return self._default_after

    def set_default_run_id(self, run_id: str | None) -> None:
        self._default_run_id = run_id

    def default_run_id(self) -> str | None:
        return self._default_run_id

    def set_current_run_id(self, run_id: str | None) -> None:
        self._current_run_id = run_id

    def retained_count(self) -> int:
        return len(self._events)

    async def subscribe(self, after: int | None = None) -> AsyncIterator[dict[str, Any]]:
        cursor = after or 0
        while True:
            async with self._condition:
                while True:
                    event = self._first_event_after(cursor)
                    if event is not None:
                        cursor = int(event.get("event_id") or cursor)
                        break
                    await self._condition.wait()
            yield dict(event)

    def _first_event_after(self, after: int) -> dict[str, Any] | None:
        for event in self._events:
            if int(event.get("event_id") or 0) > after:
                return event
        return None

    def _trim_retained_events(self) -> None:
        self._trim_transient_events()
        if len(self._events) <= self._max_events:
            return
        del self._events[: len(self._events) - self._max_events]

    def _trim_transient_events(self) -> None:
        transient_indexes = [index for index, event in enumerate(self._events) if event.get("type") == "assistant.delta"]
        excess = len(transient_indexes) - MAX_TRANSIENT_RETAINED_EVENTS
        if excess <= 0:
            return
        drop = set(transient_indexes[:excess])
        self._events = [event for index, event in enumerate(self._events) if index not in drop]


@dataclass(slots=True)
class Session:
    session_id: str
    workspace: str
    language: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    events: SessionEvents = field(default_factory=SessionEvents)
    messages: list[dict[str, Any]] = field(default_factory=list)
    approvals: dict[str, PendingApproval] = field(default_factory=dict)
    agent_queue: asyncio.Queue[QueuedAgentRun] = field(default_factory=asyncio.Queue)
    agent_runner_task: asyncio.Task[Any] | None = None
    auto_accept_edits: bool = False
    message_appender: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "language": self.language,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "messages": self.messages,
            "approvals": [approval.to_dict() for approval in self.approvals.values()],
            "agent": {
                "running": self.agent_runner_active(),
                "queued": self.agent_queue.qsize(),
            },
        }

    def enqueue_agent_run(self, request: Any) -> QueuedAgentRun:
        queued = QueuedAgentRun(run_id=f"run_{uuid4().hex[:12]}", request=request)
        self.agent_queue.put_nowait(queued)
        return queued

    def append_message(self, message: dict[str, Any]) -> None:
        if self.message_appender is not None:
            self.message_appender(message)
            return
        self.messages.append(message)

    def next_agent_run(self) -> QueuedAgentRun | None:
        try:
            return self.agent_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def finish_agent_run(self) -> None:
        self.agent_queue.task_done()

    def agent_runner_active(self) -> bool:
        return self.agent_runner_task is not None and not self.agent_runner_task.done()

    def create_approval(self, kind: str, payload: dict[str, Any]) -> PendingApproval:
        approval = PendingApproval(
            approval_id=f"appr_{uuid4().hex[:12]}",
            kind=kind,
            payload=payload,
        )
        self.approvals[approval.approval_id] = approval
        return approval

    def resolve_approval(self, approval_id: str, accepted: bool) -> bool:
        approval = self.approvals.get(approval_id)
        if approval is None or approval.accepted is not None:
            return False
        approval.accepted = accepted
        approval.decision_event.set()
        return True

    async def wait_for_approval(self, approval_id: str, timeout_seconds: float = 300.0) -> bool | None:
        approval = self.approvals.get(approval_id)
        if approval is None:
            return None
        try:
            await asyncio.wait_for(approval.decision_event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return None
        return approval.accepted


class SessionStore:
    def __init__(self, path: Path | None = None, event_limit: int | None = None, cache_limit: int | None = None) -> None:
        self.path = path or default_session_db_path()
        self.event_limit = normalize_event_limit(event_limit)
        self._cache_limit = normalize_cache_limit(cache_limit)
        self._sessions: dict[str, Session] = {}
        self._last_session_id: str | None = None
        self._schema_ready = False
        self._write_queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._event_writes_enqueued = 0
        self._event_writes_written = 0
        self._event_writes_dropped = 0
        self._event_writes_failed = 0

    def _touch(self, session: Session) -> None:
        """把 session 标记为最近使用，并在超出缓存上限时驱逐最久未用的可驱逐 session。

        驱逐只丢弃内存中的 Session 对象（SessionEvents 缓冲、待决 approval 的
        asyncio.Event、agent_queue）；SQLite 里的 session/message/event 行不受影响。
        再次 get() 会从数据库重新构建一个新的 Session 对象，行为等同于 daemon 重启后
        首次访问这个 session——已有的读路径本就支持这种情况。
        """
        self._sessions.pop(session.session_id, None)
        self._sessions[session.session_id] = session
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        if len(self._sessions) <= self._cache_limit:
            return
        for session_id in list(self._sessions.keys()):
            if len(self._sessions) <= self._cache_limit:
                return
            candidate = self._sessions[session_id]
            if self._is_evictable(candidate):
                del self._sessions[session_id]

    def _is_evictable(self, session: Session) -> bool:
        if session.agent_runner_active():
            return False
        if any(approval.accepted is None for approval in session.approvals.values()):
            return False
        return True

    def create(self, workspace: str, language: str) -> Session:
        self._ensure_schema()
        session = Session(
            session_id=f"sess_{uuid4().hex[:12]}",
            workspace=workspace,
            language=language,
        )
        self._attach_events(session)
        session.updated_at = session.created_at
        self._touch(session)
        self._last_session_id = session.session_id
        self._insert_session(session)
        return session

    def get(self, session_id: str) -> Session | None:
        self._ensure_schema()
        cached = self._sessions.get(session_id)
        if cached is not None:
            self._touch(cached)
            return cached

        with self._connect() as conn:
            row = conn.execute(
                "select session_id, workspace, language, created_at, updated_at from sessions where session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            session = self._session_from_row(row)
            session.messages = self._load_messages(conn, session.session_id)
            self._attach_events(session, self._load_events(conn, session.session_id))
            self._touch(session)
            return session

    def list(self) -> list[dict[str, Any]]:
        self._ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                "select rowid, session_id, workspace, language, created_at, updated_at from sessions order by updated_at desc, rowid desc"
            ).fetchall()
            sessions: list[dict[str, Any]] = []
            for row in rows:
                session = self._sessions.get(row["session_id"])
                if session is None:
                    session = self._session_from_row(row)
                    self._attach_events(session, self._load_events(conn, session.session_id))
                session.messages = self._load_messages(conn, session.session_id)
                self._touch(session)
                sessions.append(session.to_dict())
            return sessions

    def last(self) -> Session | None:
        self._ensure_schema()
        if self._last_session_id:
            cached = self.get(self._last_session_id)
            if cached is not None:
                return cached

        with self._connect() as conn:
            row = conn.execute(
                "select rowid, session_id, workspace, language, created_at, updated_at from sessions order by updated_at desc, rowid desc limit 1"
            ).fetchone()
            if row is None:
                return None
            session = self._session_from_row(row)
            session.messages = self._load_messages(conn, session.session_id)
            self._attach_events(session, self._load_events(conn, session.session_id))
            self._touch(session)
            self._last_session_id = session.session_id
            return session

    def append_message(self, session: Session, message: dict[str, Any]) -> None:
        self._ensure_schema()
        session.updated_at = datetime.now(timezone.utc)
        self._last_session_id = session.session_id
        session.messages.append(message)
        with self._connect() as conn:
            conn.execute(
                "insert into messages (session_id, role, payload, created_at) values (?, ?, ?, ?)",
                (
                    session.session_id,
                    str(message.get("role") or "user"),
                    json.dumps(message, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.execute(
                "update sessions set updated_at = ? where session_id = ?",
                (session.updated_at.isoformat(), session.session_id),
            )

    def event_writer_status(self) -> dict[str, Any]:
        return {
            "queue_size": self._write_queue.qsize() if self._write_queue is not None else 0,
            "queue_max_size": EVENT_WRITE_QUEUE_MAXSIZE,
            "writer_running": self._writer_task is not None and not self._writer_task.done(),
            "enqueued": self._event_writes_enqueued,
            "written": self._event_writes_written,
            "dropped": self._event_writes_dropped,
            "failed": self._event_writes_failed,
        }

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists sessions (
                    session_id text primary key,
                    workspace text not null,
                    language text not null,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists messages (
                    id integer primary key autoincrement,
                    session_id text not null,
                    role text not null,
                    payload text not null,
                    created_at text not null,
                    foreign key (session_id) references sessions(session_id)
                );

                create index if not exists idx_messages_session_id on messages(session_id, id);

                create table if not exists events (
                    id integer primary key autoincrement,
                    session_id text not null,
                    sequence integer not null,
                    payload text not null,
                    created_at text not null,
                    foreign key (session_id) references sessions(session_id)
                );

                create unique index if not exists idx_events_session_sequence on events(session_id, sequence);
            """
            )
            self._ensure_updated_at_column(conn)
        self._schema_ready = True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert_session(self, session: Session) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into sessions (session_id, workspace, language, created_at, updated_at)
                values (?, ?, ?, ?, ?)
                """,
                (session.session_id, session.workspace, session.language, session.created_at.isoformat(), session.updated_at.isoformat()),
            )

    def _session_from_row(self, row: sqlite3.Row) -> Session:
        return Session(
            session_id=str(row["session_id"]),
            workspace=str(row["workspace"]),
            language=str(row["language"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def _attach_events(self, session: Session, events: list[dict[str, Any]] | None = None) -> None:
        session.events = SessionEvents(
            events=events,
            on_event=lambda event: self._append_event(session.session_id, event),
            max_events=self.event_limit,
        )
        session.message_appender = lambda message: self.append_message(session, message)

    def _append_event(self, session_id: str, event: dict[str, Any]) -> None:
        sequence = int(event.get("event_id") or 0)
        if sequence <= 0:
            return
        self._ensure_writer()
        assert self._write_queue is not None
        try:
            self._write_queue.put_nowait((session_id, dict(event)))
            self._event_writes_enqueued += 1
        except asyncio.QueueFull:
            # 事件落盘是尽力而为：队列打满时丢弃这条写入，不阻塞 agent loop。
            self._event_writes_dropped += 1

    def _ensure_writer(self) -> None:
        if self._writer_task is not None and not self._writer_task.done():
            return
        self._write_queue = asyncio.Queue(maxsize=EVENT_WRITE_QUEUE_MAXSIZE)
        self._writer_task = asyncio.get_running_loop().create_task(self._event_writer_loop())

    async def _event_writer_loop(self) -> None:
        queue = self._write_queue
        assert queue is not None
        while True:
            session_id, event = await queue.get()
            try:
                await asyncio.to_thread(self._write_event_sync, session_id, event)
                self._event_writes_written += 1
            except Exception:
                # 审计/回放数据丢失不应中断 agent loop；单条写入失败不影响后续事件。
                self._event_writes_failed += 1
            finally:
                queue.task_done()

    def _write_event_sync(self, session_id: str, event: dict[str, Any]) -> None:
        self._ensure_schema()
        sequence = int(event.get("event_id") or 0)
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into events (session_id, sequence, payload, created_at)
                values (?, ?, ?, ?)
                """,
                (
                    session_id,
                    sequence,
                    json.dumps(event, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._prune_events(conn, session_id)

    async def flush(self) -> None:
        """等待后台写入队列中的所有事件被处理完（成功或失败）。

        测试用它来确定性地等待异步落盘完成；FastAPI 的 lifespan shutdown 钩子
        用它在进程退出前排空队列，避免丢失刚发生但还没来得及落盘的事件。
        """
        if self._write_queue is not None:
            await self._write_queue.join()

    async def aclose(self) -> None:
        """Flush pending event writes and stop the background writer task."""
        await self.flush()
        task = self._writer_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._writer_task = None
        self._write_queue = None

    def _prune_events(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute(
            """
            delete from events
            where session_id = ?
              and sequence not in (
                  select sequence from events
                  where session_id = ?
                  order by sequence desc
                  limit ?
              )
            """,
            (session_id, session_id, self.event_limit),
        )

    def _load_messages(self, conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "select payload from messages where session_id = ? order by id asc",
            (session_id,),
        ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            try:
                messages.append(json.loads(str(row["payload"])))
            except json.JSONDecodeError:
                continue
        return messages

    def _load_events(self, conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            select sequence, payload
            from (
                select sequence, payload
                from events
                where session_id = ?
                order by sequence desc
                limit ?
            )
            order by sequence asc
            """,
            (session_id, self.event_limit),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                event = json.loads(str(row["payload"]))
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event.setdefault("event_id", int(row["sequence"]))
            events.append(event)
        return events

    def _ensure_updated_at_column(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("pragma table_info(sessions)").fetchall()}
        if "updated_at" in columns:
            return
        conn.execute("alter table sessions add column updated_at text")
        conn.execute("update sessions set updated_at = created_at where updated_at is null")


def default_session_db_path() -> Path:
    explicit = os.getenv("AICODE_SESSION_DB_PATH")
    if explicit:
        return Path(explicit)

    home = os.getenv("AICODE_HOME")
    if home:
        return Path(home) / "sessions.sqlite"

    home_path = Path.home() / ".aicode" / "sessions.sqlite"
    try:
        home_path.parent.mkdir(parents=True, exist_ok=True)
        return home_path
    except OSError:
        return Path(os.getenv("TMPDIR", "/tmp")) / "aicode" / "sessions.sqlite"


def normalize_event_limit(value: int | None = None) -> int:
    if value is None:
        raw = os.getenv("AICODE_SESSION_EVENT_LIMIT")
        if not raw:
            return DEFAULT_SESSION_EVENT_LIMIT
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_SESSION_EVENT_LIMIT
    return max(1, int(value))


def normalize_cache_limit(value: int | None = None) -> int:
    if value is None:
        raw = os.getenv("AICODE_SESSION_CACHE_LIMIT")
        if not raw:
            return DEFAULT_SESSION_CACHE_LIMIT
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_SESSION_CACHE_LIMIT
    return max(1, int(value))


store = SessionStore()
