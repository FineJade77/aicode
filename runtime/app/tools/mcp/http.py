"""MCP over Streamable HTTP.

The same JSON-RPC conversation as the stdio transport, carried over POSTs to a
single endpoint. A server may answer a request either with a plain JSON body or
with an SSE stream that eventually carries the response, so both shapes are
handled.

An HTTP server is further away than a subprocess, and that distance is the whole
security story here:

- the URL comes from the repository's own config, so it is only reachable at all
  because the workspace is trusted, and it must never be able to redirect the
  request — with a bearer token attached — to some other host;
- the response is attacker-controlled bytes of unbounded length, so it is read
  under a cap rather than into memory in full;
- credentials are named, never written down: the config may point at an
  environment variable, and the value itself never appears in the repository.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.tools.mcp.protocol import (
    CLIENT_CAPABILITIES,
    CLIENT_INFO,
    PROTOCOL_VERSION,
    McpProtocolError,
    notification,
    request,
    result_of,
)

DEFAULT_STARTUP_TIMEOUT_SECONDS = 20.0
DEFAULT_CALL_TIMEOUT_SECONDS = 60.0
# One MCP response is a tool result, not a download. The cap keeps a hostile or
# broken server from growing the daemon's memory; the tool layer truncates again
# for the model, but that happens after the bytes are already here.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SESSION_HEADER = "mcp-session-id"
PROTOCOL_HEADER = "MCP-Protocol-Version"


class HttpMcpServer:
    """One MCP server reached over Streamable HTTP.

    Presents the same surface as `StdioMcpServer` so the manager does not branch
    on transport: `start`, `list_tools`, `call_tool`, `stop`, `running`.
    """

    def __init__(
        self,
        name: str,
        url: str,
        *,
        auth_token_env: str = "",
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        call_timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        scheme = url.split("://", 1)[0].casefold() if "://" in url else ""
        if scheme not in {"http", "https"}:
            raise McpProtocolError(f"MCP server {name!r} url must be http or https, got {url!r}")
        self.name = name
        self.url = url
        self.auth_token_env = auth_token_env
        self.startup_timeout = startup_timeout
        self.call_timeout = call_timeout
        # `follow_redirects=False` is the point, not a default left alone: a
        # redirect would re-send the Authorization header to whatever host the
        # server named, which is the one thing a bearer token must never do.
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self._session_id = ""
        self._next_id = 0
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    async def start(self) -> list[dict[str, Any]]:
        await self._handshake()
        self._started = True
        return await self.list_tools()

    async def _handshake(self) -> None:
        await self._call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": CLIENT_CAPABILITIES,
                "clientInfo": CLIENT_INFO,
            },
            timeout=self.startup_timeout,
        )
        await self._notify("notifications/initialized")

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._call("tools/list", timeout=self.startup_timeout)
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McpProtocolError("tools/list did not return a list")
        return [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._call(
            "tools/call",
            {"name": tool, "arguments": arguments},
            timeout=self.call_timeout,
        )

    async def stop(self) -> None:
        self._started = False
        if self._owns_client:
            await self._client.aclose()

    def stderr_tail(self) -> str:
        """No stderr over HTTP. Present so failure reporting stays transport-blind."""
        return ""

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Both shapes are acceptable; the server picks.
            "Accept": "application/json, text/event-stream",
            PROTOCOL_HEADER: PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self.auth_token_env:
            # Named, not stored. The repository config carries the variable's
            # name; the secret itself never appears in a file the repo controls.
            token = os.environ.get(self.auth_token_env, "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float) -> dict[str, Any]:
        self._next_id += 1
        message = request(self._next_id, method, params)
        response = await self._post(message, timeout=timeout)
        payload = await self._read_response(response, self._next_id)
        return result_of(payload)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        response = await self._post(notification(method, params), timeout=self.startup_timeout)
        await response.aclose()

    async def _post(self, message: dict[str, Any], *, timeout: float) -> httpx.Response:
        try:
            response = await self._client.post(
                self.url,
                json=message,
                headers=self._headers(),
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"MCP server {self.name!r} did not respond within {timeout}s") from exc
        except httpx.HTTPError as exc:
            raise McpProtocolError(f"MCP server {self.name!r} request failed: {exc}") from exc
        if response.is_redirect:
            raise McpProtocolError(
                f"MCP server {self.name!r} answered with a redirect; aicode does not follow one "
                "because it would re-send credentials to another host"
            )
        if response.status_code >= 400:
            raise McpProtocolError(f"MCP server {self.name!r} returned HTTP {response.status_code}")
        # The server assigns a session on initialize and expects it echoed back.
        session_id = response.headers.get(SESSION_HEADER, "")
        if session_id:
            self._session_id = session_id
        return response

    async def _read_response(self, response: httpx.Response, request_id: int) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "").split(";")[0].strip().casefold()
        body = await self._read_bounded(response)
        if content_type == "text/event-stream":
            return _response_from_sse(body, request_id, self.name)
        if content_type == "application/json":
            return _decode_object(body, self.name)
        raise McpProtocolError(
            f"MCP server {self.name!r} answered with unsupported content type {content_type!r}"
        )

    async def _read_bounded(self, response: httpx.Response) -> str:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                await response.aclose()
                raise McpProtocolError(
                    f"MCP server {self.name!r} sent more than {MAX_RESPONSE_BYTES} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")


def _decode_object(body: str, server: str) -> dict[str, Any]:
    try:
        message = json.loads(body)
    except json.JSONDecodeError as exc:
        raise McpProtocolError(f"MCP server {server!r} sent a body that is not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise McpProtocolError(f"MCP server {server!r} sent a JSON value that is not an object")
    return message


def _response_from_sse(body: str, request_id: int, server: str) -> dict[str, Any]:
    """Pull the JSON-RPC response for `request_id` out of an SSE body.

    A server may interleave notifications and progress events on the stream, so
    the matching id is what ends the read — not simply the first data frame.
    """
    for block in body.split("\n\n"):
        data = "\n".join(
            line[len("data:") :].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        )
        if not data:
            continue
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            # A frame aicode cannot parse is skipped rather than fatal: the
            # response it is waiting for may still be further down the stream.
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise McpProtocolError(
        f"MCP server {server!r} closed its event stream without answering request {request_id}"
    )
