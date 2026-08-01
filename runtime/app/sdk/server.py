"""stdio JSONL RPC server — the embedding transport.

Sits on the same `ApplicationRuntime` the HTTP server does, so an embedder gets
the identical session, run, approval and policy semantics rather than a second
implementation that drifts. The only thing that differs is the pipe.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from app.application.contracts import TurnRequest
from app.application.errors import ApplicationError, NotFound
from app.sdk.protocol import (
    ERR_INTERNAL,
    ERR_INVALID_REQUEST,
    ERR_NOT_FOUND,
    ERR_NOT_INITIALIZED,
    ERR_PARSE,
    ERR_UNKNOWN_METHOD,
    MIN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    Request,
    RpcError,
    error_response,
    negotiate,
    notification,
    response,
)

# Outbound queue depth. Bounded on purpose: a host that stops reading its pipe
# must not be able to grow the Runtime's memory without limit. When it fills,
# the event pump blocks — real backpressure — and the session's own bounded
# buffer absorbs the rest, trimming oldest. That trimming is detectable rather
# than silent: `event_id` is a per-session monotonic sequence, so a gap in it
# tells the host exactly what it missed, and `session.events` replays from a
# cursor to fill it.
OUTBOUND_QUEUE_SIZE = 256

# How long shutdown waits for queued replies to reach the host before giving up.
DRAIN_TIMEOUT_SECONDS = 5.0


class StdioServer:
    def __init__(self, runtime: Any, *, queue_size: int = OUTBOUND_QUEUE_SIZE) -> None:
        self.runtime = runtime
        self.outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self.protocol_version: int | None = None
        self._pumps: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    # --- lifecycle ----------------------------------------------------------

    async def serve(self, reader: Any, writer: Any) -> None:
        """Run until the input stream ends."""
        writer_task = asyncio.create_task(self._write_loop(writer))
        try:
            async for line in reader:
                text = line.decode("utf-8") if isinstance(line, bytes) else str(line)
                text = text.strip()
                if not text:
                    continue
                await self._handle_line(text)
        finally:
            self._closed = True
            for task in list(self._pumps.values()):
                task.cancel()
            for task in list(self._pumps.values()):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            # Drain before cancelling the writer. Input ending does not mean the
            # replies to it have been written yet — cancelling here would
            # silently discard the response to the last request the host sent.
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(self.outbound.join(), timeout=DRAIN_TIMEOUT_SECONDS)
            writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer_task

    async def _write_loop(self, writer: Any) -> None:
        while True:
            message = await self.outbound.get()
            line = json.dumps(message, ensure_ascii=False) + "\n"
            writer.write(line.encode("utf-8") if hasattr(writer, "drain") else line)
            drain = getattr(writer, "drain", None)
            if drain is not None:
                await drain()
            elif hasattr(writer, "flush"):
                writer.flush()
            self.outbound.task_done()

    async def _emit(self, message: dict[str, Any]) -> None:
        if self._closed:
            return
        await self.outbound.put(message)

    # --- dispatch -----------------------------------------------------------

    async def _handle_line(self, text: str) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            await self._emit(error_response(None, RpcError(ERR_PARSE, f"invalid JSON: {exc}")))
            return
        try:
            request = Request.parse(payload)
        except RpcError as exc:
            await self._emit(error_response(payload.get("id") if isinstance(payload, dict) else None, exc))
            return

        try:
            result = await self._dispatch(request)
        except RpcError as exc:
            if request.id is not None:
                await self._emit(error_response(request.id, exc))
            return
        except ApplicationError as exc:
            if request.id is not None:
                await self._emit(error_response(request.id, _from_application_error(exc)))
            return
        except Exception as exc:  # A handler bug must not take the pipe down.
            if request.id is not None:
                await self._emit(
                    error_response(request.id, RpcError(ERR_INTERNAL, f"{exc.__class__.__name__}: {exc}"))
                )
            return
        # A notification (no id) is executed and its result discarded — there is
        # nothing for the host to correlate a reply to.
        if request.id is not None:
            await self._emit(response(request.id, result))

    async def _dispatch(self, request: Request) -> dict[str, Any]:
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = self._methods().get(request.method)
        if handler is None:
            raise RpcError(ERR_UNKNOWN_METHOD, f"unknown method: {request.method}")
        if request.method != "initialize" and self.protocol_version is None:
            # Refusing here rather than assuming a default keeps the handshake
            # meaningful: a host that skipped it has not agreed to anything.
            raise RpcError(ERR_NOT_INITIALIZED, "initialize must be called before any other method")
        return await handler(request.params)

    def _methods(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]:
        return {
            "initialize": self.initialize,
            "session.create": self.session_create,
            "session.get": self.session_get,
            "session.prompt": self.session_prompt,
            "session.cancel": self.session_cancel,
            "session.subscribe": self.session_subscribe,
            "session.events": self.session_events,
            "approval.resolve": self.approval_resolve,
        }

    # --- methods ------------------------------------------------------------

    async def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        self.protocol_version = negotiate(params.get("protocol_version"))
        return {
            "protocol_version": self.protocol_version,
            "supported_min": MIN_PROTOCOL_VERSION,
            "supported_max": PROTOCOL_VERSION,
            "methods": sorted(self._methods()),
        }

    async def session_create(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace = _require_str(params, "workspace")
        snapshot = await self.runtime.session_service.create(workspace)
        return {"session_id": snapshot.session_id, "workspace": snapshot.workspace}

    async def session_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.runtime.session_service.get(_require_str(params, "session_id")).to_dict()

    async def session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(_require_str(params, "session_id"))
        turn = TurnRequest(
            message=_require_str(params, "message"),
            mode=str(params.get("mode") or "default"),
            workspace=str(params.get("workspace") or session.workspace),
            model=params.get("model"),
        )
        bound = self.runtime.session_service.bind_turn(session, turn)
        return (await self.runtime.runs.submit(session, bound)).to_dict()

    async def session_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(_require_str(params, "session_id"))
        control = await self.runtime.runs.cancel(session)
        return control.to_dict()

    async def session_events(self, params: dict[str, Any]) -> dict[str, Any]:
        """Replay buffered events from a cursor.

        The recovery path for a host that saw an `event_id` gap: subscribe
        delivers live events, this fills what the bounded buffer trimmed while
        the host was not reading.
        """
        session = self._require_session(_require_str(params, "session_id"))
        after = params.get("after")
        events = session.events.events_after(int(after) if after is not None else 0)
        return {"events": events, "last_event_id": session.events.last_event_id()}

    async def session_subscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = _require_str(params, "session_id")
        session = self._require_session(session_id)
        after = params.get("after")
        if session_id in self._pumps:
            # Idempotent: a second subscribe replaces the cursor rather than
            # running two pumps that would both deliver every event.
            self._pumps.pop(session_id).cancel()
        cursor = int(after) if after is not None else session.events.last_event_id()
        self._pumps[session_id] = asyncio.create_task(self._pump(session_id, session, cursor))
        return {"session_id": session_id, "after": cursor}

    async def approval_resolve(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(_require_str(params, "session_id"))
        accepted = params.get("accepted")
        if not isinstance(accepted, bool):
            raise RpcError(ERR_INVALID_REQUEST, "accepted must be a boolean")
        return self.runtime.approvals.resolve(
            session, _require_str(params, "approval_id"), accepted=accepted
        ).to_dict()

    # --- event pump ---------------------------------------------------------

    async def _pump(self, session_id: str, session: Any, after: int) -> None:
        cursor = after
        while not self._closed:
            events = session.events.events_after(cursor)
            if not events:
                await asyncio.sleep(0.01)
                continue
            for event in events:
                cursor = max(cursor, int(event.get("event_id") or cursor))
                # `put` blocks when the queue is full. That is the backpressure:
                # the pump stalls instead of buffering without limit, and the
                # session's own bounded buffer takes the strain.
                await self._emit(notification("event", {"session_id": session_id, "event": event}))

    # --- helpers ------------------------------------------------------------

    def _require_session(self, session_id: str) -> Any:
        try:
            return self.runtime.session_service.require(session_id)
        except NotFound as exc:
            raise RpcError(ERR_NOT_FOUND, str(exc)) from exc


def _require_str(params: dict[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RpcError(ERR_INVALID_REQUEST, f"{name} is required and must be a non-empty string")
    return value


def _from_application_error(exc: ApplicationError) -> RpcError:
    code = ERR_NOT_FOUND if isinstance(exc, NotFound) else ERR_INVALID_REQUEST
    return RpcError(code, str(exc))


async def serve_stdio(runtime: Any) -> None:
    """Bind the server to this process's stdin/stdout."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    await StdioServer(runtime).serve(reader, writer)
