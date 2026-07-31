from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.agent.ports import Clock, IdGenerator
from app.agent.session import COMPACTION_SCHEMA_VERSION as CORE_COMPACTION_SCHEMA_VERSION
from app.agent.session import (
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    MAX_PLAN_ITEMS,
    PLAN_ITEM_STATUSES,
    ApprovalDecision,
    CompactionEntry,
    PlanItem,
)
from app.agent.summary import StructuredSummary
from app.events import validate_event
from app.security import redact_known_environment_secrets
from app.system import SystemClock, UuidGenerator

DEFAULT_SESSION_EVENT_LIMIT = 2_000
MAX_TRANSIENT_RETAINED_EVENTS = 200
EVENT_WRITE_QUEUE_MAXSIZE = 5_000
DEFAULT_SESSION_CACHE_LIMIT = 200
COMPACTION_SCHEMA_VERSION = CORE_COMPACTION_SCHEMA_VERSION


def parse_structured_column(raw: Any) -> StructuredSummary | None:
    """Rebuild a stored structured summary, tolerating anything malformed.

    A summary is derived state: `messages` remains the source of truth, so a row
    that cannot be parsed degrades to "no structure to carry forward" rather
    than failing the session load.
    """
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return StructuredSummary(
        goal=str(payload.get("goal") or ""),
        constraints=[str(item) for item in payload.get("constraints") or [] if isinstance(item, str)],
        done=[str(item) for item in payload.get("done") or [] if isinstance(item, str)],
        pending=[str(item) for item in payload.get("pending") or [] if isinstance(item, str)],
        files_touched=[str(item) for item in payload.get("files_touched") or [] if isinstance(item, str)],
        open_failures=[str(item) for item in payload.get("open_failures") or [] if isinstance(item, str)],
    )


def parse_plan(raw: Any) -> list[PlanItem]:
    """Rebuild a persisted plan, dropping anything malformed.

    A plan is model-authored display state, not a correctness-critical record, so
    an unreadable entry is skipped rather than failing the whole session load.
    """
    if not raw:
        return []
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    items: list[PlanItem] = []
    for entry in payload[:MAX_PLAN_ITEMS]:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        status = str(entry.get("status") or "pending")
        if not text or status not in PLAN_ITEM_STATUSES:
            continue
        items.append(PlanItem(text=text, status=status))  # type: ignore[arg-type]
    return items


def _parsed_timestamp(value: str) -> datetime:
    """Parse a stored ISO timestamp, treating anything unreadable as ancient.

    A row whose timestamp cannot be parsed is already corrupt; treating it as old
    makes retention able to clean it up rather than tripping over it forever.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def idle_agent_state() -> dict[str, Any]:
    """Run state for a session that is not resident in memory.

    Only a cached session can hold a running task, so an absent cache entry is
    authoritative evidence that nothing is in flight.
    """
    return {
        "running": False,
        "queued": 0,
        "pending_steers": 0,
        "current_run_id": None,
        "stage": None,
        "started_at": None,
        "last_progress_at": None,
        "elapsed_seconds": None,
        "stalled_seconds": None,
    }


class SchemaVersionError(RuntimeError):
    """The database was written by a newer aicode than this one understands.

    Refusing is deliberate. Continuing against an unknown schema risks writing
    rows an older build cannot read back, or silently ignoring columns a newer
    build depends on.
    """


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    kind: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decision_event: asyncio.Event = field(default_factory=asyncio.Event)
    accepted: bool | None = None
    # "accepted" | "rejected" | "timed_out" | "cancelled". Distinguishes an
    # explicit refusal from one nobody answered.
    resolution: str = ""
    # The user's text for requests that ask something rather than gate something.
    # An approval answers "may I?"; a question answers "which one?", and a bool
    # cannot carry that.
    response: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "kind": self.kind,
            "status": "pending" if self.accepted is None else (self.resolution or ("accepted" if self.accepted else "rejected")),
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
        self._events = [redact_known_environment_secrets(dict(event)) for event in events or []]
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
        event = redact_known_environment_secrets(dict(event))
        validate_event(event)
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

    async def subscribe(
        self,
        after: int | None = None,
        *,
        idle_timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any] | None]:
        """Yield events after `cursor`, or None each time `idle_timeout` elapses.

        The idle tick lets the caller emit an SSE keep-alive and re-check whether
        the run it is following can still produce events. Without it a stream
        waiting on a run that will never emit again blocks until the client's own
        timeout, and the CLI treats that as a retryable disconnect and reconnects
        forever.
        """
        cursor = after or 0
        while True:
            idle = False
            async with self._condition:
                while True:
                    event = self._first_event_after(cursor)
                    if event is not None:
                        cursor = int(event.get("event_id") or cursor)
                        break
                    if idle_timeout is None:
                        await self._condition.wait()
                        continue
                    try:
                        await asyncio.wait_for(self._condition.wait(), timeout=idle_timeout)
                    except TimeoutError:
                        idle = True
                        break
            # Yielding happens outside the condition so the lock is never held
            # across a suspension point.
            if idle:
                yield None
                continue
            yield dict(event)

    def final_event_for_run(self, run_id: str) -> dict[str, Any] | None:
        """The retained terminal event for a run, if it is still in the buffer."""
        for event in reversed(self._events):
            if event.get("type") == "final" and str(event.get("run_id") or "") == run_id:
                return dict(event)
        return None

    def has_events_for_run(self, run_id: str) -> bool:
        return any(str(event.get("run_id") or "") == run_id for event in self._events)

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
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    events: SessionEvents = field(default_factory=SessionEvents)
    messages: list[dict[str, Any]] = field(default_factory=list)
    message_ids: list[int] = field(default_factory=list)
    compactions: list[CompactionEntry] = field(default_factory=list)
    approvals: dict[str, PendingApproval] = field(default_factory=dict)
    agent_queue: asyncio.Queue[QueuedAgentRun] = field(default_factory=asyncio.Queue)
    steer_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    agent_runner_task: asyncio.Task[Any] | None = None
    current_run_id: str | None = None
    current_run_stage: str | None = None
    current_run_started_at: datetime | None = None
    current_run_last_progress_at: datetime | None = None
    auto_accept_edits: bool = False
    plan: list[PlanItem] = field(default_factory=list)
    # Files this session has read, and the content hash seen at read time.
    # Deliberately in-memory only: the guarantee being enforced is "the model has
    # seen this file's current content in this conversation". After a restart the
    # safe default is to re-read, so persisting the record would weaken it.
    read_files: dict[str, str] = field(default_factory=dict)
    plan_writer: Callable[[list[PlanItem]], None] | None = field(default=None, repr=False)
    message_appender: Callable[[dict[str, Any]], int | None] | None = field(default=None, repr=False)
    compaction_appender: Callable[[CompactionEntry], CompactionEntry] | None = field(default=None, repr=False)
    clock: Clock = field(default_factory=SystemClock, repr=False)
    ids: IdGenerator = field(default_factory=UuidGenerator, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "messages": self.messages,
            "approvals": [approval.to_dict() for approval in self.approvals.values()],
            "plan": [item.to_dict() for item in self.plan],
            "agent": self.agent_state(),
        }

    def agent_state(self) -> dict[str, Any]:
        """Run state only, so session listings can report it without hydrating history."""
        now = self.clock.now()
        elapsed_seconds = None
        stalled_seconds = None
        if self.current_run_started_at is not None:
            elapsed_seconds = max(0, int((now - self.current_run_started_at).total_seconds()))
        if self.current_run_last_progress_at is not None:
            stalled_seconds = max(0, int((now - self.current_run_last_progress_at).total_seconds()))
        return {
            "running": self.agent_runner_active(),
            "queued": self.agent_queue.qsize(),
            "pending_steers": self.steer_queue.qsize(),
            "current_run_id": self.current_run_id,
            "stage": self.current_run_stage,
            "started_at": self.current_run_started_at.isoformat() if self.current_run_started_at else None,
            "last_progress_at": self.current_run_last_progress_at.isoformat() if self.current_run_last_progress_at else None,
            "elapsed_seconds": elapsed_seconds,
            "stalled_seconds": stalled_seconds,
        }

    def enqueue_agent_run(self, request: Any) -> QueuedAgentRun:
        queued = QueuedAgentRun(run_id=self.ids.new("run"), request=request)
        self.agent_queue.put_nowait(queued)
        return queued

    def queued_run_ids(self) -> set[str]:
        """Run ids waiting to start, so a stream can tell "not yet" from "gone"."""
        pending = getattr(self.agent_queue, "_queue", None)
        if pending is None:
            return set()
        return {str(getattr(item, "run_id", "")) for item in pending}

    def enqueue_steer(self, message: str) -> int:
        self.steer_queue.put_nowait(message)
        return self.steer_queue.qsize()

    def drain_steers(self) -> list[str]:
        messages: list[str] = []
        while True:
            try:
                messages.append(self.steer_queue.get_nowait())
                self.steer_queue.task_done()
            except asyncio.QueueEmpty:
                return messages

    def record_read(self, path: str, content_hash: str) -> None:
        self.read_files[path] = content_hash

    def read_hash(self, path: str) -> str | None:
        return self.read_files.get(path)

    def forget_read(self, path: str) -> None:
        self.read_files.pop(path, None)

    def set_plan(self, items: list[PlanItem]) -> list[PlanItem]:
        """Replace the whole plan.

        Whole replacement rather than incremental edits: an incremental API would
        require the model to maintain stable item ids across turns, which in
        practice goes wrong more often than the saved tokens are worth.
        """
        self.plan = list(items[:MAX_PLAN_ITEMS])
        if self.plan_writer is not None:
            self.plan_writer(self.plan)
        return self.plan

    def append_message(self, message: dict[str, Any]) -> int | None:
        if self.message_appender is not None:
            return self.message_appender(message)
        self.messages.append(message)
        message_id = (self.message_ids[-1] if self.message_ids else 0) + 1
        self.message_ids.append(message_id)
        return message_id

    def append_compaction(self, entry: CompactionEntry) -> CompactionEntry:
        if self.compaction_appender is not None:
            return self.compaction_appender(entry)
        self.compactions.append(entry)
        return entry

    def next_agent_run(self) -> QueuedAgentRun | None:
        try:
            return self.agent_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def start_agent_run(self, run_id: str) -> None:
        now = self.clock.now()
        self.current_run_id = run_id
        self.current_run_stage = "starting"
        self.current_run_started_at = now
        self.current_run_last_progress_at = now

    def mark_agent_progress(self, stage: str) -> None:
        if self.current_run_id is None:
            return
        self.current_run_stage = stage
        self.current_run_last_progress_at = self.clock.now()

    def finish_agent_run(self) -> None:
        self.agent_queue.task_done()
        self.current_run_id = None
        self.current_run_stage = None
        self.current_run_started_at = None
        self.current_run_last_progress_at = None

    def agent_runner_active(self) -> bool:
        return self.agent_runner_task is not None and not self.agent_runner_task.done()

    def create_approval(self, kind: str, payload: dict[str, Any]) -> PendingApproval:
        approval = PendingApproval(
            approval_id=self.ids.new("appr"),
            kind=kind,
            payload=payload,
            created_at=self.clock.now(),
        )
        self.approvals[approval.approval_id] = approval
        return approval

    def resolve_approval(
        self,
        approval_id: str,
        accepted: bool,
        *,
        resolution: str = "",
        response: str = "",
    ) -> bool:
        approval = self.approvals.get(approval_id)
        if approval is None or approval.accepted is not None:
            return False
        approval.accepted = accepted
        approval.response = response
        approval.resolution = resolution or ("accepted" if accepted else "rejected")
        approval.decision_event.set()
        return True

    def expire_approval(self, approval_id: str, *, resolution: str = "cancelled") -> bool:
        return self.resolve_approval(approval_id, accepted=False, resolution=resolution)

    def expire_pending_approvals(self) -> list[PendingApproval]:
        expired: list[PendingApproval] = []
        for approval in self.approvals.values():
            if approval.accepted is None and self.expire_approval(approval.approval_id):
                expired.append(approval)
        return expired

    async def wait_for_approval(
        self,
        approval_id: str,
        timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ) -> ApprovalDecision:
        approval = self.approvals.get(approval_id)
        if approval is None:
            return ApprovalDecision.MISSING
        try:
            await asyncio.wait_for(approval.decision_event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            # Recorded as a distinct resolution, not as a rejection: nobody
            # decided, so the model must not be told the user refused.
            self.expire_approval(approval_id, resolution="timed_out")
            return ApprovalDecision.TIMED_OUT
        if approval.resolution == "timed_out":
            return ApprovalDecision.TIMED_OUT
        return ApprovalDecision.ACCEPTED if approval.accepted else ApprovalDecision.REJECTED


class SessionStore:
    def __init__(
        self,
        path: Path | None = None,
        event_limit: int | None = None,
        cache_limit: int | None = None,
        *,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self.path = path or default_session_db_path()
        self.clock = clock or SystemClock()
        self.ids = ids or UuidGenerator()
        self.event_limit = normalize_event_limit(event_limit)
        self._cache_limit = normalize_cache_limit(cache_limit)
        self._sessions: dict[str, Session] = {}
        self._last_session_id: str | None = None
        self._schema_ready = False
        self._db_lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._write_queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._event_writes_enqueued = 0
        self._event_writes_written = 0
        self._event_writes_dropped = 0
        self._event_writes_failed = 0

    def _touch(self, session: Session) -> None:
        """Mark a session as recently used and evict the oldest eligible session when over capacity.

        Eviction discards only the in-memory Session object, including its SessionEvents buffer,
        pending approval asyncio.Event objects, and agent_queue. SQLite session, message, and event
        rows remain intact. A later get() rebuilds a fresh Session from the database, just as it
        would on the first access after a daemon restart.
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

    def create(self, workspace: str) -> Session:
        self._ensure_schema()
        session = Session(
            session_id=self.ids.new("sess"),
            workspace=workspace,
            created_at=self.clock.now(),
            updated_at=self.clock.now(),
            clock=self.clock,
            ids=self.ids,
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
                "select session_id, workspace, created_at, updated_at, plan from sessions where session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            session = self._session_from_row(row)
            session.messages, session.message_ids = self._load_message_records(conn, session.session_id)
            session.compactions = self._load_compactions(conn, session.session_id)
            self._attach_events(session, self._load_events_with_recovered_approvals(conn, session.session_id))
            self._touch(session)
            return session

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """Return session summaries, newest first.

        Deliberately does not load messages, compactions or events. The previous
        implementation hydrated every message of every session on each call —
        50 sessions x 40 messages measured 69ms and ~82KB per row, growing
        without bound — even though the only consumer (`aicode session list`) shows a
        listing. Full history is available from `get(session_id)`.

        Summaries are also not written into the in-memory cache: listing is a
        read-only overview and must not evict live sessions.
        """
        self._ensure_schema()
        with self._connect() as conn:
            sql = "select session_id, workspace, created_at, updated_at from sessions order by updated_at desc, rowid desc"
            params: list[Any] = []
            if limit is not None:
                sql += " limit ? offset ?"
                params.extend([max(0, limit), max(0, offset)])
            elif offset:
                sql += " limit -1 offset ?"
                params.append(max(0, offset))
            rows = conn.execute(sql, params).fetchall()
            counts = self._message_counts(conn, [str(row["session_id"]) for row in rows])

        summaries: list[dict[str, Any]] = []
        for row in rows:
            session_id = str(row["session_id"])
            cached = self._sessions.get(session_id)
            summary = {
                "session_id": session_id,
                "workspace": str(row["workspace"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"] or row["created_at"]),
                "message_count": counts.get(session_id, 0),
            }
            # Only a cached session can be running, so an absent cache entry is
            # authoritative evidence that the session is idle.
            summary["agent"] = cached.agent_state() if cached is not None else idle_agent_state()
            summaries.append(summary)
        return summaries

    def prune(self, *, max_sessions: int | None = None, max_age_days: int | None = None) -> dict[str, Any]:
        """Delete old sessions and everything that hangs off them.

        Retention is opt-in (both bounds default to disabled). Silently deleting
        a user's conversation history is worse than an unbounded database, so
        nothing is removed unless a bound is configured or passed explicitly.

        A session that is live in memory is never deleted, even if it matches a
        bound: an in-flight run or an unresolved approval would lose the state it
        is about to write back.
        """
        self._ensure_schema()
        max_sessions = None if not max_sessions or max_sessions < 0 else max_sessions
        max_age_days = None if not max_age_days or max_age_days < 0 else max_age_days
        if max_sessions is None and max_age_days is None:
            return {"status": "disabled", "deleted_sessions": 0, "deleted_messages": 0, "retained_live": 0}

        protected = {
            session_id
            for session_id, session in self._sessions.items()
            if not self._is_evictable(session)
        }

        with self._connect() as conn:
            rows = conn.execute(
                "select session_id, updated_at from sessions order by updated_at desc, rowid desc"
            ).fetchall()

            doomed: list[str] = []
            if max_age_days is not None:
                cutoff = self.clock.now() - timedelta(days=max_age_days)
                for row in rows:
                    if _parsed_timestamp(str(row["updated_at"])) < cutoff:
                        doomed.append(str(row["session_id"]))
            if max_sessions is not None and len(rows) > max_sessions:
                doomed.extend(str(row["session_id"]) for row in rows[max_sessions:])

            retained_live = sorted({session_id for session_id in doomed if session_id in protected})
            targets = sorted({session_id for session_id in doomed if session_id not in protected})
            if not targets:
                return {
                    "status": "ok",
                    "deleted_sessions": 0,
                    "deleted_messages": 0,
                    "retained_live": len(retained_live),
                }

            placeholders = ",".join("?" for _ in targets)
            deleted_messages = conn.execute(
                f"select count(*) from messages where session_id in ({placeholders})", targets
            ).fetchone()[0]
            for table in ("messages", "events", "compactions"):
                conn.execute(f"delete from {table} where session_id in ({placeholders})", targets)
            conn.execute(f"delete from sessions where session_id in ({placeholders})", targets)

        for session_id in targets:
            self._sessions.pop(session_id, None)
            if self._last_session_id == session_id:
                self._last_session_id = None

        return {
            "status": "ok",
            "deleted_sessions": len(targets),
            "deleted_messages": int(deleted_messages),
            "retained_live": len(retained_live),
        }

    @staticmethod
    def _message_counts(conn: sqlite3.Connection, session_ids: list[str]) -> dict[str, int]:
        """One grouped query rather than a COUNT per session."""
        if not session_ids:
            return {}
        placeholders = ",".join("?" for _ in session_ids)
        rows = conn.execute(
            f"select session_id, count(*) as total from messages where session_id in ({placeholders}) group by session_id",
            session_ids,
        ).fetchall()
        return {str(row["session_id"]): int(row["total"]) for row in rows}

    def last(self) -> Session | None:
        self._ensure_schema()
        if self._last_session_id:
            cached = self.get(self._last_session_id)
            if cached is not None:
                return cached

        with self._connect() as conn:
            row = conn.execute(
                "select rowid, session_id, workspace, created_at, updated_at, plan from sessions order by updated_at desc, rowid desc limit 1"
            ).fetchone()
            if row is None:
                return None
            session = self._session_from_row(row)
            session.messages, session.message_ids = self._load_message_records(conn, session.session_id)
            session.compactions = self._load_compactions(conn, session.session_id)
            self._attach_events(session, self._load_events_with_recovered_approvals(conn, session.session_id))
            self._touch(session)
            self._last_session_id = session.session_id
            return session

    def write_plan(self, session: Session, items: list[PlanItem]) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            conn.execute(
                "update sessions set plan = ? where session_id = ?",
                (json.dumps([item.to_dict() for item in items], ensure_ascii=False), session.session_id),
            )

    def append_message(self, session: Session, message: dict[str, Any]) -> int:
        self._ensure_schema()
        session.updated_at = self.clock.now()
        self._last_session_id = session.session_id
        with self._connect() as conn:
            cursor = conn.execute(
                "insert into messages (session_id, role, payload, created_at) values (?, ?, ?, ?)",
                (
                    session.session_id,
                    str(message.get("role") or "user"),
                    json.dumps(message, ensure_ascii=False),
                    self.clock.now().isoformat(),
                ),
            )
            conn.execute(
                "update sessions set updated_at = ? where session_id = ?",
                (session.updated_at.isoformat(), session.session_id),
            )
            message_id = int(cursor.lastrowid)
        session.messages.append(message)
        session.message_ids.append(message_id)
        return message_id

    def append_compaction(self, session: Session, entry: CompactionEntry) -> CompactionEntry:
        self._ensure_schema()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                insert into compactions (
                    session_id, schema_version, start_message_id, end_message_id, summary,
                    provider, model, prompt_version, before_tokens, after_tokens,
                    context_window, created_at, structured
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    entry.schema_version,
                    entry.start_message_id,
                    entry.end_message_id,
                    entry.summary,
                    entry.provider,
                    entry.model,
                    entry.prompt_version,
                    entry.before_tokens,
                    entry.after_tokens,
                    entry.context_window,
                    entry.created_at.isoformat(),
                    json.dumps(entry.structured.to_dict(), ensure_ascii=False) if entry.structured is not None else None,
                ),
            )
            stored = CompactionEntry(
                compaction_id=int(cursor.lastrowid),
                session_id=session.session_id,
                schema_version=entry.schema_version,
                start_message_id=entry.start_message_id,
                end_message_id=entry.end_message_id,
                summary=entry.summary,
                provider=entry.provider,
                model=entry.model,
                prompt_version=entry.prompt_version,
                before_tokens=entry.before_tokens,
                after_tokens=entry.after_tokens,
                context_window=entry.context_window,
                created_at=entry.created_at,
                structured=entry.structured,
            )
        session.compactions.append(stored)
        return stored

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
            current = int(conn.execute("pragma user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"session database at {self.path} uses schema version {current}, but this "
                    f"aicode build only understands up to {SCHEMA_VERSION}. Upgrade aicode, or point "
                    f"AICODE_SESSION_DB_PATH at a different database."
                )
            for version, migrate in MIGRATIONS:
                if version > current:
                    migrate(conn)
            if current < SCHEMA_VERSION:
                # Not parameterised because PRAGMA does not accept bindings; the
                # value is our own module constant, never user input.
                conn.execute(f"pragma user_version = {SCHEMA_VERSION}")
        self._schema_ready = True

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield the shared connection inside a transaction.

        Previously every call opened a fresh connection in rollback-journal mode
        with `synchronous=FULL`, which cost ~5ms per message write — all of it on
        the event loop, competing with the SSE stream that is pushing
        `assistant.delta` to the user at the same time. Reusing one WAL
        connection removes both the setup cost and the per-transaction fsync.

        The connection is shared across the event loop and the write-behind
        worker thread, so `check_same_thread=False` is paired with a lock held
        for the whole transaction rather than just the statement.
        """
        with self._db_lock:
            conn = self._ensure_connection()
            with conn:
                yield conn

    def _ensure_connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL lets readers proceed during a write, which matters because list()
        # and resume read while a run is still appending messages.
        conn.execute("pragma journal_mode=WAL")
        # NORMAL is the standard durability trade under WAL: a crash can lose the
        # most recent commits, but the database is never corrupted. Sessions are
        # recoverable local state, not a system of record.
        conn.execute("pragma synchronous=NORMAL")
        # Wait rather than fail immediately when another connection holds a lock.
        conn.execute("pragma busy_timeout=5000")
        self._conn = conn
        return conn

    def _close_connection(self) -> None:
        with self._db_lock:
            if self._conn is not None:
                with suppress(Exception):
                    self._conn.close()
            self._conn = None

    def _insert_session(self, session: Session) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into sessions (session_id, workspace, created_at, updated_at)
                values (?, ?, ?, ?)
                """,
                (session.session_id, session.workspace, session.created_at.isoformat(), session.updated_at.isoformat()),
            )

    def _session_from_row(self, row: sqlite3.Row) -> Session:
        return Session(
            session_id=str(row["session_id"]),
            workspace=str(row["workspace"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            plan=parse_plan(row["plan"] if "plan" in row.keys() else None),
            clock=self.clock,
            ids=self.ids,
        )

    def _attach_events(self, session: Session, events: list[dict[str, Any]] | None = None) -> None:
        session.events = SessionEvents(
            events=events,
            on_event=lambda event: self._append_event(session.session_id, event),
            max_events=self.event_limit,
        )
        session.message_appender = lambda message: self.append_message(session, message)
        session.plan_writer = lambda items: self.write_plan(session, items)
        session.compaction_appender = lambda entry: self.append_compaction(session, entry)

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
            # Event persistence is best-effort; drop writes when full instead of blocking the agent loop.
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
                # Missing audit/replay data must not interrupt the loop; one failed write does not block later events.
                self._event_writes_failed += 1
            finally:
                queue.task_done()

    def _write_event_sync(self, session_id: str, event: dict[str, Any]) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            self._write_event_with_connection(conn, session_id, event)
            self._prune_events(conn, session_id)

    def _write_event_with_connection(self, conn: sqlite3.Connection, session_id: str, event: dict[str, Any]) -> None:
        sequence = int(event.get("event_id") or 0)
        if sequence <= 0:
            return
        conn.execute(
            """
            insert or ignore into events (session_id, sequence, payload, created_at)
            values (?, ?, ?, ?)
            """,
            (
                session_id,
                sequence,
                json.dumps(event, ensure_ascii=False),
                self.clock.now().isoformat(),
            ),
        )

    async def flush(self) -> None:
        """Wait until every queued event has been processed, whether successfully or not.

        Tests use this to wait deterministically for asynchronous persistence. FastAPI's lifespan
        shutdown hook drains the queue before exit so newly emitted events are not lost.
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
        self._close_connection()

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

    def _load_message_records(self, conn: sqlite3.Connection, session_id: str) -> tuple[list[dict[str, Any]], list[int]]:
        rows = conn.execute(
            "select id, payload from messages where session_id = ? order by id asc",
            (session_id,),
        ).fetchall()
        messages: list[dict[str, Any]] = []
        message_ids: list[int] = []
        for row in rows:
            try:
                message = json.loads(str(row["payload"]))
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            messages.append(message)
            message_ids.append(int(row["id"]))
        return messages, message_ids

    def _load_compactions(self, conn: sqlite3.Connection, session_id: str) -> list[CompactionEntry]:
        rows = conn.execute(
            """
            select id, session_id, schema_version, start_message_id, end_message_id,
                   summary, provider, model, prompt_version, before_tokens, after_tokens,
                   context_window, created_at, structured
            from compactions
            where session_id = ?
            order by id asc
            """,
            (session_id,),
        ).fetchall()
        return [
            CompactionEntry(
                compaction_id=int(row["id"]),
                session_id=str(row["session_id"]),
                schema_version=int(row["schema_version"]),
                start_message_id=int(row["start_message_id"]),
                end_message_id=int(row["end_message_id"]),
                summary=str(row["summary"]),
                provider=str(row["provider"]),
                model=str(row["model"]),
                prompt_version=str(row["prompt_version"]),
                before_tokens=int(row["before_tokens"]),
                after_tokens=int(row["after_tokens"]),
                context_window=int(row["context_window"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                structured=parse_structured_column(row["structured"]),
            )
            for row in rows
        ]

    def _load_events_with_recovered_approvals(self, conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
        events = self._load_events(conn, session_id)
        recovered = self._approval_recovery_events(events)
        for event in recovered:
            self._write_event_with_connection(conn, session_id, event)
        return events + recovered

    def _approval_recovery_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: dict[str, dict[str, Any]] = {}
        by_tool_call_id: dict[str, set[str]] = {}
        for event in events:
            event_type = str(event.get("type") or "")
            approval_id = str(event.get("approval_id") or "")
            tool_call_id = str(event.get("tool_call_id") or "")
            if event_type == "approval.requested" and approval_id:
                pending[approval_id] = event
                if tool_call_id:
                    by_tool_call_id.setdefault(tool_call_id, set()).add(approval_id)
                continue
            if event_type == "approval.expired" and approval_id:
                pending.pop(approval_id, None)
                continue
            if tool_call_id and event_type in {"tool.output", "tool.error", "tool.rejected", "edit.applied", "edit.rejected"}:
                for resolved_id in by_tool_call_id.get(tool_call_id, set()):
                    pending.pop(resolved_id, None)

        next_event_id = max((int(event.get("event_id") or 0) for event in events), default=0) + 1
        recovered: list[dict[str, Any]] = []
        for approval in pending.values():
            approval_id = str(approval.get("approval_id") or "")
            kind = str(approval.get("kind") or "tool")
            tool_call_id = str(approval.get("tool_call_id") or "")
            message = "The pending approval expired after daemon restart or session recovery and was treated as denied."
            expired: dict[str, Any] = {
                "type": "approval.expired",
                "approval_id": approval_id,
                "kind": kind,
                "message": message,
                "event_id": next_event_id,
            }
            if tool_call_id:
                expired["tool_call_id"] = tool_call_id
            recovered.append(expired)
            next_event_id += 1

            rejected = self._approval_rejected_event(approval, message, next_event_id)
            if rejected is not None:
                recovered.append(rejected)
                next_event_id += 1
        return recovered

    def _approval_rejected_event(self, approval: dict[str, Any], message: str, event_id: int) -> dict[str, Any] | None:
        kind = str(approval.get("kind") or "tool")
        tool_call_id = str(approval.get("tool_call_id") or "")
        if kind == "edit":
            event: dict[str, Any] = {
                "type": "edit.rejected",
                "path": approval.get("path"),
                "reason": message,
                "event_id": event_id,
            }
            if tool_call_id:
                event["tool_call_id"] = tool_call_id
            return event
        tool = str(approval.get("tool") or "")
        if not tool:
            return None
        event = {"type": "tool.rejected", "tool": tool, "error": message, "event_id": event_id}
        if tool_call_id:
            event["tool_call_id"] = tool_call_id
        return event

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

def _migration_001_base_schema(conn: sqlite3.Connection) -> None:
    """Base tables and indexes.

    Kept idempotent (`if not exists`) because databases created before the
    migration ladder existed carry `user_version = 0` while already holding
    these tables, and must land on the ladder without being recreated.
    """
    conn.executescript(
        """
        create table if not exists sessions (
            session_id text primary key,
            workspace text not null,
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

        create table if not exists compactions (
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
            created_at text not null,
            structured text,
            foreign key (session_id) references sessions(session_id)
        );

        create index if not exists idx_compactions_session_id on compactions(session_id, id);

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


def _migration_002_sessions_updated_at(conn: sqlite3.Connection) -> None:
    """Add `sessions.updated_at`, backfilled from `created_at`."""
    columns = {row["name"] for row in conn.execute("pragma table_info(sessions)").fetchall()}
    if "updated_at" in columns:
        return
    conn.execute("alter table sessions add column updated_at text")
    conn.execute("update sessions set updated_at = created_at where updated_at is null")


def _migration_003_drop_sessions_language(conn: sqlite3.Connection) -> None:
    """Drop the retired `sessions.language` column (English-only Runtime)."""
    columns = {row["name"] for row in conn.execute("pragma table_info(sessions)").fetchall()}
    if "language" not in columns:
        return
    conn.execute(
        """
        create table sessions_without_language (
            session_id text primary key,
            workspace text not null,
            created_at text not null,
            updated_at text not null
        )
        """
    )
    conn.execute(
        """
        insert into sessions_without_language (rowid, session_id, workspace, created_at, updated_at)
        select rowid, session_id, workspace, created_at, updated_at from sessions
        """
    )
    conn.execute("drop table sessions")
    conn.execute("alter table sessions_without_language rename to sessions")


# Ordered ladder. Append only: never renumber or edit a shipped migration, since
# databases in the field record how far they have already been advanced.
def _migration_004_sessions_plan(conn: sqlite3.Connection) -> None:
    """Add `sessions.plan`, holding the model's externalised plan as JSON."""
    columns = {row["name"] for row in conn.execute("pragma table_info(sessions)").fetchall()}
    if "plan" in columns:
        return
    conn.execute("alter table sessions add column plan text")


def _migration_005_compactions_structured(conn: sqlite3.Connection) -> None:
    """Add `compactions.structured`, holding the structured summary as JSON."""
    columns = {row["name"] for row in conn.execute("pragma table_info(compactions)").fetchall()}
    if "structured" in columns:
        return
    conn.execute("alter table compactions add column structured text")


MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migration_001_base_schema),
    (2, _migration_002_sessions_updated_at),
    (3, _migration_003_drop_sessions_language),
    (4, _migration_004_sessions_plan),
    (5, _migration_005_compactions_structured),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


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
