from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.audit.redaction import redact
from app.security import stable_hash as _stable_hash

AUDIT_WRITE_QUEUE_MAXSIZE = 5_000
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

# Backward-compatible import path; new code imports from app.security.
stable_hash = _stable_hash


class AuditLogger:
    """Append-only audit trail with bounded size and no silent loss.

    Two properties matter more here than in an ordinary log:

    * **No silent drops.** The audit trail is the security evidence chain. If a
      record can vanish under load, "no record of a dangerous command" becomes
      indistinguishable from "no dangerous command happened". When the async
      write queue is full this class therefore falls back to writing inline
      rather than discarding the event: a rare blocking append is a better
      trade than an unnoticed hole in the evidence.
    * **Bounded growth.** A daemon that runs for months cannot append to one
      file forever, so writes rotate by size with a fixed number of backups.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
    ) -> None:
        self.path = path
        self.max_bytes = max(0, max_bytes)
        self.backup_count = max(0, backup_count)
        self._write_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._file_lock = threading.Lock()
        self._handle: Any = None
        self._handle_path: Path | None = None
        self._writes_enqueued = 0
        self._writes_written = 0
        self._writes_inline = 0
        self._writes_failed = 0
        self._last_error = ""
        self._reported_failure = False

    @classmethod
    def from_env(cls) -> AuditLogger:
        explicit = os.getenv("AICODE_AUDIT_PATH")
        if explicit:
            path = Path(explicit)
        else:
            home = os.getenv("AICODE_HOME")
            path = Path(home) / "audit.jsonl" if home else Path.home() / ".aicode" / "audit.jsonl"
        return cls(
            path,
            max_bytes=_int_env("AICODE_AUDIT_MAX_BYTES", DEFAULT_MAX_BYTES),
            backup_count=_int_env("AICODE_AUDIT_BACKUP_COUNT", DEFAULT_BACKUP_COUNT),
        )

    def record(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        workspace: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "session_id": session_id,
            "workspace": workspace,
            "data": redact(data or {}),
        }
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._write_event(event)
            return

        self._ensure_writer()
        assert self._write_queue is not None
        try:
            self._write_queue.put_nowait(event)
            self._writes_enqueued += 1
        except asyncio.QueueFull:
            # Never drop an audit record. A full queue means the writer cannot
            # keep up, which is already pathological; a blocking append is the
            # correct trade against losing evidence.
            self._writes_inline += 1
            self._write_event(event)

    def status(self) -> dict[str, Any]:
        return {
            "queue_size": self._write_queue.qsize() if self._write_queue is not None else 0,
            "queue_max_size": AUDIT_WRITE_QUEUE_MAXSIZE,
            "writer_running": self._writer_task is not None and not self._writer_task.done(),
            "enqueued": self._writes_enqueued,
            "written": self._writes_written,
            "inline": self._writes_inline,
            "failed": self._writes_failed,
            "healthy": self._writes_failed == 0,
            "last_error": self._last_error,
            "max_bytes": self.max_bytes,
            "backup_count": self.backup_count,
        }

    async def flush(self) -> None:
        if self._write_queue is not None:
            await self._write_queue.join()

    async def aclose(self) -> None:
        await self.flush()
        task = self._writer_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._writer_task = None
        self._write_queue = None
        self._close_handle()

    def _ensure_writer(self) -> None:
        if self._writer_task is not None and not self._writer_task.done():
            return
        self._write_queue = asyncio.Queue(maxsize=AUDIT_WRITE_QUEUE_MAXSIZE)
        self._writer_task = asyncio.get_running_loop().create_task(self._writer_loop())

    async def _writer_loop(self) -> None:
        queue = self._write_queue
        assert queue is not None
        while True:
            event = await queue.get()
            try:
                await asyncio.to_thread(self._write_event, event)
            finally:
                queue.task_done()

    def _write_event(self, event: dict[str, Any]) -> None:
        """Serialize one event, retrying once before reporting a failure.

        Never raises: an exception escaping here would either kill the writer
        task or abort an agent turn. Failures are counted, surfaced through
        status(), and reported once on stderr so a broken audit path cannot go
        unnoticed while the daemon keeps running.
        """
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        for attempt in range(2):
            try:
                with self._file_lock:
                    handle = self._open_handle()
                    handle.write(line)
                    handle.flush()
                    self._maybe_rotate_locked()
                self._writes_written += 1
                return
            except Exception as exc:  # noqa: BLE001 - audit must not break callers
                with self._file_lock:
                    self._close_handle_locked()
                if attempt == 0:
                    continue
                self._writes_failed += 1
                self._last_error = f"{exc.__class__.__name__}: {exc}"
                if not self._reported_failure:
                    self._reported_failure = True
                    print(
                        f"aicode: audit log write failed ({self._last_error}); "
                        f"the audit trail at {self.path} is incomplete",
                        file=sys.stderr,
                        flush=True,
                    )

    def _open_handle(self):
        if self._handle is not None and self._handle_path == self.path and not self._handle.closed:
            return self._handle
        self._close_handle_locked()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self._handle_path = self.path
        return self._handle

    def _maybe_rotate_locked(self) -> None:
        if self.max_bytes <= 0 or self._handle is None:
            return
        if self._handle.tell() < self.max_bytes:
            return
        self._close_handle_locked()
        if self.backup_count == 0:
            self.path.unlink(missing_ok=True)
            return
        # Shift audit.jsonl.N-1 -> audit.jsonl.N, dropping the oldest.
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    def _close_handle(self) -> None:
        with self._file_lock:
            self._close_handle_locked()

    def _close_handle_locked(self) -> None:
        if self._handle is not None:
            with suppress(Exception):
                self._handle.close()
        self._handle = None
        self._handle_path = None


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value >= 0 else default
