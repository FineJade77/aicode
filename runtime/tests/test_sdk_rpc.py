"""stdio JSONL RPC and the Python SDK.

The transport sits on the same `ApplicationRuntime` the HTTP server uses, so
these tests are about the protocol — handshake, framing, error mapping,
backpressure — not about re-proving session or run semantics.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.application.contracts import SessionSnapshot
from app.application.errors import NotFound
from app.sdk.client import AgentClient
from app.sdk.protocol import (
    MIN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    Request,
    RpcError,
    negotiate,
)
from app.sdk.server import StdioServer


class FakeWriter:
    def __init__(self) -> None:
        self.lines: list[dict] = []

    def write(self, data) -> None:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        for line in text.splitlines():
            if line.strip():
                self.lines.append(json.loads(line))

    def flush(self) -> None:
        pass


async def run_server(server: StdioServer, requests: list[dict], writer: FakeWriter) -> None:
    async def lines():
        for request in requests:
            yield (json.dumps(request) + "\n").encode("utf-8")

    await server.serve(lines(), writer)


def replies(writer: FakeWriter) -> list[dict]:
    return [line for line in writer.lines if "id" in line]


# --- protocol -----------------------------------------------------------------


def test_a_request_needs_a_method():
    with pytest.raises(RpcError, match="method"):
        Request.parse({"id": "1"})


def test_params_default_to_empty_rather_than_none():
    assert Request.parse({"method": "initialize"}).params == {}


def test_params_must_be_an_object():
    with pytest.raises(RpcError, match="params"):
        Request.parse({"method": "x", "params": [1, 2]})


def test_negotiation_accepts_the_supported_range():
    assert negotiate(PROTOCOL_VERSION) == PROTOCOL_VERSION
    assert negotiate(None) == PROTOCOL_VERSION


def test_negotiation_refuses_rather_than_downgrading():
    """A host that asked for a version we cannot speak has features in mind.

    Silently serving an older one moves the failure to a later call, where it is
    far harder to attribute than a rejected handshake.
    """
    with pytest.raises(RpcError) as excinfo:
        negotiate(PROTOCOL_VERSION + 1)

    assert excinfo.value.data == {
        "supported_min": MIN_PROTOCOL_VERSION,
        "supported_max": PROTOCOL_VERSION,
    }


def test_a_boolean_is_not_a_protocol_version():
    """`True` is an int in Python — accepting it would negotiate version 1 by accident."""
    with pytest.raises(RpcError, match="integer"):
        negotiate(True)


# --- server dispatch ----------------------------------------------------------


class FakeRuntime:
    def __init__(self, tmp_path: Path) -> None:
        self.workspace = str(tmp_path)
        self.session_service = self
        self.created: list[str] = []

    async def create(self, workspace: str) -> SessionSnapshot:
        self.created.append(workspace)
        return SessionSnapshot.from_mapping(
            {
                "session_id": "sess_1",
                "workspace": workspace,
                "created_at": "",
                "updated_at": "",
                "messages": [],
                "approvals": [],
                "agent": {},
            }
        )

    def require(self, session_id: str):
        raise NotFound("session not found")


@pytest.mark.asyncio
async def test_initialize_must_come_first(tmp_path):
    writer = FakeWriter()
    server = StdioServer(FakeRuntime(tmp_path))

    await run_server(server, [{"id": "1", "method": "session.create", "params": {"workspace": "/x"}}], writer)

    assert replies(writer)[0]["error"]["code"] == "not_initialized"


@pytest.mark.asyncio
async def test_the_handshake_advertises_the_method_set(tmp_path):
    writer = FakeWriter()
    server = StdioServer(FakeRuntime(tmp_path))

    await run_server(server, [{"id": "1", "method": "initialize", "params": {}}], writer)

    result = replies(writer)[0]["result"]
    assert result["protocol_version"] == PROTOCOL_VERSION
    assert "session.prompt" in result["methods"]


@pytest.mark.asyncio
async def test_an_unknown_method_is_an_error_not_a_crash(tmp_path):
    writer = FakeWriter()
    server = StdioServer(FakeRuntime(tmp_path))

    await run_server(
        server,
        [{"id": "1", "method": "initialize"}, {"id": "2", "method": "nope"}],
        writer,
    )

    assert replies(writer)[1]["error"]["code"] == "unknown_method"


@pytest.mark.asyncio
async def test_malformed_json_is_reported_and_the_stream_continues(tmp_path):
    """One bad line must not end the session — the next request still works."""
    writer = FakeWriter()
    server = StdioServer(FakeRuntime(tmp_path))

    async def lines():
        yield b"{not json\n"
        yield (json.dumps({"id": "2", "method": "initialize"}) + "\n").encode()

    await server.serve(lines(), writer)

    assert replies(writer)[0]["error"]["code"] == "parse_error"
    assert "result" in replies(writer)[1]


@pytest.mark.asyncio
async def test_a_notification_gets_no_reply(tmp_path):
    """No id means nothing to correlate a response to, so none is written."""
    writer = FakeWriter()
    server = StdioServer(FakeRuntime(tmp_path))

    await run_server(server, [{"method": "initialize"}], writer)

    assert replies(writer) == []


@pytest.mark.asyncio
async def test_a_missing_session_maps_to_not_found(tmp_path):
    writer = FakeWriter()
    server = StdioServer(FakeRuntime(tmp_path))

    await run_server(
        server,
        [
            {"id": "1", "method": "initialize"},
            {"id": "2", "method": "session.cancel", "params": {"session_id": "nope"}},
        ],
        writer,
    )

    assert replies(writer)[1]["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_a_missing_required_param_is_rejected(tmp_path):
    writer = FakeWriter()
    server = StdioServer(FakeRuntime(tmp_path))

    await run_server(
        server,
        [{"id": "1", "method": "initialize"}, {"id": "2", "method": "session.create", "params": {}}],
        writer,
    )

    assert replies(writer)[1]["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_session_create_reaches_the_application_runtime(tmp_path):
    writer = FakeWriter()
    runtime = FakeRuntime(tmp_path)
    server = StdioServer(runtime)

    await run_server(
        server,
        [
            {"id": "1", "method": "initialize"},
            {"id": "2", "method": "session.create", "params": {"workspace": "/repo"}},
        ],
        writer,
    )

    assert runtime.created == ["/repo"]
    assert replies(writer)[1]["result"]["session_id"] == "sess_1"


# --- backpressure -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_outbound_queue_is_bounded(tmp_path):
    """A host that stops reading must not grow the Runtime's memory without limit.

    The queue blocks instead: `_emit` does not return once it is full, which is
    what stalls the event pump rather than buffering forever.
    """
    server = StdioServer(FakeRuntime(tmp_path), queue_size=2)

    await server._emit({"a": 1})
    await server._emit({"a": 2})

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(server._emit({"a": 3}), timeout=0.05)


@pytest.mark.asyncio
async def test_emitting_after_close_is_a_no_op(tmp_path):
    """Otherwise a pump racing shutdown blocks forever on a queue nobody drains."""
    server = StdioServer(FakeRuntime(tmp_path), queue_size=1)
    server._closed = True

    await asyncio.wait_for(server._emit({"a": 1}), timeout=0.05)
    await asyncio.wait_for(server._emit({"a": 2}), timeout=0.05)


# --- client / server round trip ----------------------------------------------


class Pipe:
    """An in-memory duplex pipe, so the round trip needs no subprocess."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._buffer = b""

    def write(self, data) -> None:
        self.queue.put_nowait(data if isinstance(data, bytes) else data.encode())

    def flush(self) -> None:
        pass

    async def readline(self) -> bytes:
        while b"\n" not in self._buffer:
            chunk = await self.queue.get()
            if not chunk:
                return b""  # EOF sentinel — the Runtime closed the pipe.
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line + b"\n"

    async def __aiter__(self):
        while True:
            yield await self.readline()


@pytest.mark.asyncio
async def test_client_and_server_complete_a_handshake(tmp_path):
    to_server, to_client = Pipe(), Pipe()
    server = StdioServer(FakeRuntime(tmp_path))
    server_task = asyncio.create_task(server.serve(to_server, to_client))
    client = AgentClient(to_client, to_server)
    await client.start()

    try:
        result = await asyncio.wait_for(client.initialize(), timeout=2)
        session_id = await asyncio.wait_for(client.create_session("/repo"), timeout=2)
    finally:
        await client.aclose()
        server_task.cancel()

    assert result["protocol_version"] == PROTOCOL_VERSION
    assert client.protocol_version == PROTOCOL_VERSION
    assert session_id == "sess_1"


@pytest.mark.asyncio
async def test_a_server_error_raises_on_the_client(tmp_path):
    to_server, to_client = Pipe(), Pipe()
    server = StdioServer(FakeRuntime(tmp_path))
    server_task = asyncio.create_task(server.serve(to_server, to_client))
    client = AgentClient(to_client, to_server)
    await client.start()

    try:
        await asyncio.wait_for(client.initialize(), timeout=2)
        with pytest.raises(RpcError) as excinfo:
            await asyncio.wait_for(client.call("nope"), timeout=2)
    finally:
        await client.aclose()
        server_task.cancel()

    assert excinfo.value.code == "unknown_method"


@pytest.mark.asyncio
async def test_a_closed_runtime_fails_in_flight_calls(tmp_path):
    """Otherwise the caller awaits a reply that can never arrive."""
    to_server, to_client = Pipe(), Pipe()
    client = AgentClient(to_client, to_server)
    await client.start()

    pending = asyncio.create_task(client.call("initialize"))
    await asyncio.sleep(0.01)
    to_client.queue.put_nowait(b"")  # EOF

    with pytest.raises(RpcError, match="closed the connection"):
        await asyncio.wait_for(pending, timeout=2)
    await client.aclose()
