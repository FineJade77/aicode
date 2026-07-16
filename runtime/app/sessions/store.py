from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


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


class SessionEvents:
    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._events = [dict(event) for event in events or []]
        self._condition = asyncio.Condition()
        self._on_event = on_event
        self._read_cursor = 0
        self._next_sequence = max((int(event.get("event_id") or 0) for event in self._events), default=0) + 1
        self._default_after: int | None = None

    async def put(self, event: dict[str, Any]) -> None:
        event = dict(event)
        if "event_id" not in event:
            event["event_id"] = self._next_sequence
            self._next_sequence += 1
        else:
            self._next_sequence = max(self._next_sequence, int(event["event_id"]) + 1)

        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                pass

        async with self._condition:
            self._events.append(event)
            self._condition.notify_all()

    async def get(self) -> dict[str, Any]:
        async with self._condition:
            while self._read_cursor >= len(self._events):
                await self._condition.wait()
            event = self._events[self._read_cursor]
            self._read_cursor += 1
            return dict(event)

    def empty(self) -> bool:
        return self._read_cursor >= len(self._events)

    def events_after(self, after: int | None = None) -> list[dict[str, Any]]:
        after = after or 0
        return [dict(event) for event in self._events if int(event.get("event_id") or 0) > after]

    def last_event_id(self) -> int:
        return self._next_sequence - 1

    def set_default_after(self, after: int | None) -> None:
        self._default_after = after

    def default_after(self) -> int | None:
        return self._default_after

    async def subscribe(self, after: int | None = None) -> AsyncIterator[dict[str, Any]]:
        cursor = self._cursor_after(after)
        while True:
            async with self._condition:
                while cursor >= len(self._events):
                    await self._condition.wait()
                event = self._events[cursor]
                cursor += 1
            yield dict(event)

    def _cursor_after(self, after: int | None) -> int:
        after = after or 0
        for index, event in enumerate(self._events):
            if int(event.get("event_id") or 0) > after:
                return index
        return len(self._events)


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "language": self.language,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "messages": self.messages,
            "approvals": [approval.to_dict() for approval in self.approvals.values()],
        }

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
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_session_db_path()
        self._sessions: dict[str, Session] = {}
        self._last_session_id: str | None = None
        self._schema_ready = False

    def create(self, workspace: str, language: str) -> Session:
        self._ensure_schema()
        session = Session(
            session_id=f"sess_{uuid4().hex[:12]}",
            workspace=workspace,
            language=language,
        )
        self._attach_events(session)
        session.updated_at = session.created_at
        self._sessions[session.session_id] = session
        self._last_session_id = session.session_id
        self._insert_session(session)
        return session

    def get(self, session_id: str) -> Session | None:
        self._ensure_schema()
        if session_id in self._sessions:
            return self._sessions[session_id]

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
            self._sessions[session.session_id] = session
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
                self._sessions[session.session_id] = session
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
            self._sessions[session.session_id] = session
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
        )

    def _append_event(self, session_id: str, event: dict[str, Any]) -> None:
        self._ensure_schema()
        sequence = int(event.get("event_id") or 0)
        if sequence <= 0:
            return
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
            "select sequence, payload from events where session_id = ? order by sequence asc",
            (session_id,),
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


store = SessionStore()
