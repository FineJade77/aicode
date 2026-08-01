"""Long-running commands that outlive the tool call that started them.

`bash` blocks for at most a few minutes, so a dev server, a watch build, or a
long compile could only be run by blocking the whole turn on it — which in
practice means the model does not run them at all. A background command returns
a handle immediately; its output accumulates in a bounded buffer that later tool
calls read from, and it is killed by process group on stop or on daemon
shutdown.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from app.execution.host import build_subprocess_environment, kill_process_group

MAX_BACKGROUND_PROCESSES = 5
MAX_BUFFER_CHARS = 200_000
READ_CHUNK_BYTES = 4_096


@dataclass(slots=True)
class BackgroundProcess:
    handle_id: str
    command: str
    workspace: Path
    process: asyncio.subprocess.Process
    started_at: float
    session_id: str = ""
    reader: asyncio.Task | None = None
    buffer: str = ""
    # Absolute count of characters discarded from the front of the buffer. Reads
    # are addressed by absolute offset, so a reader that falls behind is told it
    # lost output rather than being silently handed the wrong bytes.
    dropped_chars: int = 0
    produced_chars: int = 0
    exit_code: int | None = None
    stopped: bool = False
    # Files that exist only for this process (a sandbox profile). Removed when it
    # ends, so a long-lived daemon does not accumulate them.
    cleanup_paths: tuple[Path, ...] = ()

    @property
    def running(self) -> bool:
        return self.exit_code is None and not self.stopped

    def status(self) -> str:
        # `stopped` wins over `exited`. Both are true once we kill it, but "you
        # stopped this" and "it ended on its own" are different facts, and the
        # second would let the model conclude the server crashed. The exit code
        # is reported alongside either way.
        if self.stopped:
            return "stopped"
        return "exited" if self.exit_code is not None else "running"

    def append(self, text: str) -> None:
        self.buffer += text
        self.produced_chars += len(text)
        overflow = len(self.buffer) - MAX_BUFFER_CHARS
        if overflow > 0:
            self.buffer = self.buffer[overflow:]
            self.dropped_chars += overflow

    def describe(self) -> dict[str, object]:
        return {
            "handle": self.handle_id,
            "command": self.command,
            "status": self.status(),
            "exit_code": self.exit_code,
            "running_seconds": round(time.monotonic() - self.started_at, 1),
            "buffered_chars": len(self.buffer),
            "dropped_chars": self.dropped_chars,
        }


@dataclass(slots=True)
class BackgroundReadResult:
    handle_id: str
    text: str
    next_offset: int
    dropped: int
    status: str
    exit_code: int | None


class BackgroundProcessError(Exception):
    pass


@dataclass(slots=True)
class BackgroundProcessManager:
    """Owns every background process for the lifetime of the daemon.

    Kept here rather than on the session so that daemon shutdown has a single
    place to reap from: a process parented to a session would outlive its owner
    whenever the session was evicted from cache.
    """

    processes: dict[str, BackgroundProcess] = field(default_factory=dict)

    async def start(
        self,
        command: str,
        *,
        workspace: Path,
        env_allowlist: tuple[str, ...] | None = None,
        session_id: str = "",
        cleanup_paths: tuple[Path, ...] = (),
    ) -> BackgroundProcess:
        live = [entry for entry in self.processes.values() if entry.running]
        if len(live) >= MAX_BACKGROUND_PROCESSES:
            raise BackgroundProcessError(
                f"too many background commands are already running ({len(live)}); "
                "stop one with stop_command before starting another"
            )
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise BackgroundProcessError(f"workspace does not exist or is not a directory: {workspace}")

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(workspace),
                env=build_subprocess_environment(env_allowlist, workspace=workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Its own process group, so stopping it takes the whole tree with
                # it — a dev server that spawns workers must not leave orphans.
                start_new_session=True,
            )
        except OSError as exc:
            raise BackgroundProcessError(f"{exc.__class__.__name__}: {exc}") from exc

        entry = BackgroundProcess(
            handle_id=f"bg_{uuid.uuid4().hex[:12]}",
            command=command,
            workspace=workspace,
            process=process,
            started_at=time.monotonic(),
            session_id=session_id,
            cleanup_paths=cleanup_paths,
        )
        self.processes[entry.handle_id] = entry
        entry.reader = asyncio.create_task(self._drain(entry))
        return entry

    async def _drain(self, entry: BackgroundProcess) -> None:
        """Consume output continuously.

        Not read on demand: a process whose pipe fills up blocks forever, so the
        buffer has to be drained even while nobody is reading it.
        """
        stream = entry.process.stdout
        if stream is not None:
            while True:
                try:
                    chunk = await stream.read(READ_CHUNK_BYTES)
                except (asyncio.CancelledError, ValueError):
                    raise
                if not chunk:
                    break
                entry.append(chunk.decode("utf-8", errors="replace"))
        with suppress(ProcessLookupError):
            await entry.process.wait()
        entry.exit_code = entry.process.returncode
        _remove_cleanup_paths(entry)

    def get(self, handle_id: str) -> BackgroundProcess:
        entry = self.processes.get(handle_id)
        if entry is None:
            raise BackgroundProcessError(f"no background command with handle {handle_id}")
        return entry

    def read(self, handle_id: str, *, after: int = 0) -> BackgroundReadResult:
        entry = self.get(handle_id)
        after = max(0, after)
        dropped = 0
        if after < entry.dropped_chars:
            # The reader fell behind the buffer cap. Say so instead of returning
            # a fragment that looks contiguous but is not.
            dropped = entry.dropped_chars - after
            after = entry.dropped_chars
        text = entry.buffer[after - entry.dropped_chars :]
        return BackgroundReadResult(
            handle_id=handle_id,
            text=text,
            next_offset=entry.dropped_chars + len(entry.buffer),
            dropped=dropped,
            status=entry.status(),
            exit_code=entry.exit_code,
        )

    async def stop(self, handle_id: str) -> BackgroundProcess:
        entry = self.get(handle_id)
        if entry.exit_code is None:
            kill_process_group(entry.process)
            entry.stopped = True
            if entry.reader is not None:
                with suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(asyncio.shield(entry.reader), timeout=2.0)
            if entry.exit_code is None:
                with suppress(Exception):
                    await asyncio.wait_for(entry.process.wait(), timeout=1.0)
                entry.exit_code = entry.process.returncode
        return entry

    def list(self, *, session_id: str = "") -> list[BackgroundProcess]:
        return [
            entry
            for entry in self.processes.values()
            if not session_id or entry.session_id == session_id
        ]

    async def stop_all(self) -> None:
        for handle_id in list(self.processes):
            with suppress(BackgroundProcessError):
                await self.stop(handle_id)
        for entry in self.processes.values():
            if entry.reader is not None and not entry.reader.done():
                entry.reader.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await entry.reader
            _remove_cleanup_paths(entry)
        self.processes.clear()


def _remove_cleanup_paths(entry: BackgroundProcess) -> None:
    for path in entry.cleanup_paths:
        with suppress(OSError):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
