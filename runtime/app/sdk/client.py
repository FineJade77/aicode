"""Python SDK — the embedder-facing half of the stdio RPC.

Speaks to a Runtime over a pipe, so the host process does not need to import
the Runtime, manage its dependencies, or share its event loop. That isolation is
the point of the transport: an editor plugin embeds a subprocess, not a library.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from app.sdk.protocol import PROTOCOL_VERSION, RpcError


class AgentClient:
    """Client for one Runtime process.

    Not thread-safe and not shareable across event loops — one client per
    connection, which is the same lifetime as the subprocess it talks to.
    """

    def __init__(self, reader: Any, writer: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self.protocol_version: int | None = None

    @classmethod
    async def spawn(cls, *command: str) -> AgentClient:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
        )
        client = cls(process.stdout, process.stdin)
        client._process = process  # type: ignore[attr-defined]
        await client.start()
        return client

    async def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def aclose(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        process = getattr(self, "_process", None)
        if process is not None and process.returncode is None:
            process.terminate()
            await process.wait()

    async def __aenter__(self) -> AgentClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # --- transport ----------------------------------------------------------

    async def _read_loop(self) -> None:
        while True:
            line = await self._reader.readline()
            if not line:
                # The Runtime exited. Fail every in-flight call rather than
                # leaving callers awaiting a reply that can never arrive.
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(RpcError("closed", "runtime closed the connection"))
                self._pending.clear()
                return
            message = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
            if message.get("id") is not None:
                future = self._pending.pop(str(message["id"]), None)
                if future is not None and not future.done():
                    future.set_result(message)
            else:
                await self._events.put(message)

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._next_id += 1
        request_id = str(self._next_id)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = json.dumps({"id": request_id, "method": method, "params": params}, ensure_ascii=False)
        self._writer.write((payload + "\n").encode("utf-8"))
        drain = getattr(self._writer, "drain", None)
        if drain is not None:
            await drain()
        message = await future
        if "error" in message:
            error = message["error"]
            raise RpcError(error.get("code", "internal_error"), error.get("message", ""), error.get("data"))
        return message.get("result") or {}

    # --- methods ------------------------------------------------------------

    async def initialize(self, protocol_version: int = PROTOCOL_VERSION) -> dict[str, Any]:
        result = await self.call("initialize", protocol_version=protocol_version)
        self.protocol_version = int(result["protocol_version"])
        return result

    async def create_session(self, workspace: str) -> str:
        return str((await self.call("session.create", workspace=workspace))["session_id"])

    async def subscribe(self, session_id: str, after: int | None = None) -> dict[str, Any]:
        return await self.call("session.subscribe", session_id=session_id, after=after)

    async def prompt(self, session_id: str, message: str, *, mode: str = "default") -> dict[str, Any]:
        return await self.call("session.prompt", session_id=session_id, message=message, mode=mode)

    async def cancel(self, session_id: str) -> dict[str, Any]:
        return await self.call("session.cancel", session_id=session_id)

    async def resolve_approval(self, session_id: str, approval_id: str, *, accepted: bool) -> dict[str, Any]:
        return await self.call(
            "approval.resolve", session_id=session_id, approval_id=approval_id, accepted=accepted
        )

    async def replay(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        return list((await self.call("session.events", session_id=session_id, after=after))["events"])

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield subscribed session events as they arrive.

        Check `event_id` for gaps: a jump means the Runtime's bounded buffer
        trimmed while this client was not reading, and `replay(after=...)`
        recovers the range. A gap is reported rather than hidden precisely
        because a silent one is indistinguishable from nothing having happened.
        """
        while True:
            message = await self._events.get()
            if message.get("method") == "event":
                yield message["params"]
