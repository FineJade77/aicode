from __future__ import annotations

import asyncio
import json
import os
import sqlite3
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


@dataclass(slots=True)
class Session:
    session_id: str
    workspace: str
    language: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    events: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    messages: list[dict[str, Any]] = field(default_factory=list)
    approvals: dict[str, PendingApproval] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "language": self.language,
            "created_at": self.created_at.isoformat(),
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
                "select session_id, workspace, language, created_at from sessions where session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            session = self._session_from_row(row)
            session.messages = self._load_messages(conn, session.session_id)
            self._sessions[session.session_id] = session
            return session

    def list(self) -> list[dict[str, Any]]:
        self._ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                "select rowid, session_id, workspace, language, created_at from sessions order by created_at desc, rowid desc"
            ).fetchall()
            sessions: list[dict[str, Any]] = []
            for row in rows:
                session = self._sessions.get(row["session_id"]) or self._session_from_row(row)
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
                "select rowid, session_id, workspace, language, created_at from sessions order by created_at desc, rowid desc limit 1"
            ).fetchone()
            if row is None:
                return None
            session = self._session_from_row(row)
            session.messages = self._load_messages(conn, session.session_id)
            self._sessions[session.session_id] = session
            self._last_session_id = session.session_id
            return session

    def append_message(self, session: Session, message: dict[str, Any]) -> None:
        self._ensure_schema()
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
                    created_at text not null
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
                """
            )
        self._schema_ready = True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _insert_session(self, session: Session) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into sessions (session_id, workspace, language, created_at)
                values (?, ?, ?, ?)
                """,
                (session.session_id, session.workspace, session.language, session.created_at.isoformat()),
            )

    def _session_from_row(self, row: sqlite3.Row) -> Session:
        return Session(
            session_id=str(row["session_id"]),
            workspace=str(row["workspace"]),
            language=str(row["language"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
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
