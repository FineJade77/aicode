from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.audit.redaction import redact
from app.core.hashing import stable_hash as _stable_hash


AUDIT_WRITE_QUEUE_MAXSIZE = 5_000

# Backward-compatible import path; new code imports from app.core.hashing.
stable_hash = _stable_hash


class AuditLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._write_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._writes_enqueued = 0
        self._writes_written = 0
        self._writes_dropped = 0
        self._writes_failed = 0

    @classmethod
    def from_env(cls) -> "AuditLogger":
        explicit = os.getenv("AICODE_AUDIT_PATH")
        if explicit:
            return cls(Path(explicit))

        home = os.getenv("AICODE_HOME")
        if home:
            return cls(Path(home) / "audit.jsonl")

        return cls(Path.home() / ".aicode" / "audit.jsonl")

    def record(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        workspace: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "session_id": session_id,
            "workspace": workspace,
            "data": redact(data or {}),
        }
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._write_event_sync(event)
            self._writes_written += 1
            return

        self._ensure_writer()
        assert self._write_queue is not None
        try:
            self._write_queue.put_nowait(event)
            self._writes_enqueued += 1
        except asyncio.QueueFull:
            self._writes_dropped += 1

    def status(self) -> dict[str, Any]:
        return {
            "queue_size": self._write_queue.qsize() if self._write_queue is not None else 0,
            "queue_max_size": AUDIT_WRITE_QUEUE_MAXSIZE,
            "writer_running": self._writer_task is not None and not self._writer_task.done(),
            "enqueued": self._writes_enqueued,
            "written": self._writes_written,
            "dropped": self._writes_dropped,
            "failed": self._writes_failed,
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
                await asyncio.to_thread(self._write_event_sync, event)
                self._writes_written += 1
            except Exception:
                self._writes_failed += 1
            finally:
                queue.task_done()

    def _write_event_sync(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
