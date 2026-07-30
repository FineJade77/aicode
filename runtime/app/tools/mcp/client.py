from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from app.execution.host import build_subprocess_environment
from app.tools.mcp.protocol import (
    CLIENT_CAPABILITIES,
    CLIENT_INFO,
    PROTOCOL_VERSION,
    McpProtocolError,
    decode,
    encode,
    notification,
    request,
    result_of,
)

DEFAULT_STARTUP_TIMEOUT_SECONDS = 20.0
DEFAULT_CALL_TIMEOUT_SECONDS = 60.0
SHUTDOWN_GRACE_SECONDS = 2.0
# A server that floods stderr must not be able to grow the daemon's memory.
MAX_RETAINED_STDERR_CHARS = 4_000


class StdioMcpServer:
    """One MCP server running as a child process, spoken to over stdio.

    Every call is bounded by a timeout and the process is killed as a group on
    shutdown: an MCP server is third-party code, so it is treated like any other
    subprocess the Runtime spawns rather than trusted to behave.
    """

    def __init__(
        self,
        name: str,
        command: list[str],
        *,
        cwd: Path,
        env_allowlist: tuple[str, ...] | None = None,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        call_timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self.name = name
        self.command = command
        self.cwd = cwd
        self.env_allowlist = env_allowlist
        self.startup_timeout = startup_timeout
        self.call_timeout = call_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: list[str] = []

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> list[dict[str, Any]]:
        """Launch the server, complete the handshake, and return its tool list."""
        env = build_subprocess_environment(self.env_allowlist, workspace=self.cwd)
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=str(self.cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await asyncio.wait_for(self._handshake(), timeout=self.startup_timeout)
        return await self.list_tools()

    async def _handshake(self) -> None:
        await self._call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": CLIENT_CAPABILITIES,
                "clientInfo": CLIENT_INFO,
            },
        )
        await self._notify("notifications/initialized")

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await asyncio.wait_for(self._call("tools/list"), timeout=self.startup_timeout)
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McpProtocolError("tools/list did not return a list")
        return [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.wait_for(
            self._call("tools/call", {"name": tool, "arguments": arguments}),
            timeout=self.call_timeout,
        )

    async def stop(self) -> None:
        process = self._process
        self._process = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=SHUTDOWN_GRACE_SECONDS)
        except TimeoutError:
            # A server that ignores SIGTERM must not keep the daemon from exiting.
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    def stderr_tail(self) -> str:
        return "".join(self._stderr_tail)[-MAX_RETAINED_STDERR_CHARS:]

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # One request in flight at a time. aicode issues tool calls sequentially
        # per server, so a full id-to-future router would add concurrency this
        # client never uses.
        async with self._lock:
            self._next_id += 1
            await self._write(request(self._next_id, method, params))
            while True:
                message = await self._read()
                if message.get("id") == self._next_id:
                    return result_of(message)
                # Server-initiated requests and notifications are not supported;
                # skipping them is safer than answering something unimplemented.

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write(notification(method, params))

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpProtocolError(f"MCP server {self.name!r} is not running")
        process.stdin.write(encode(message))
        await process.stdin.drain()

    async def _read(self) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise McpProtocolError(f"MCP server {self.name!r} is not running")
        line = await process.stdout.readline()
        if not line:
            detail = self.stderr_tail().strip()
            suffix = f": {detail}" if detail else ""
            raise McpProtocolError(f"MCP server {self.name!r} closed its output{suffix}")
        return decode(line)

    async def _drain_stderr(self) -> None:
        """Keep a bounded tail of stderr so failures can be explained."""
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            self._stderr_tail.append(line.decode("utf-8", errors="replace"))
            if len(self._stderr_tail) > 200:
                del self._stderr_tail[:100]
