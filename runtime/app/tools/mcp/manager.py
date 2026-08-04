from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.ports import ToolSpec
from app.tools.base import ToolContext, ToolResult
from app.tools.mcp.client import StdioMcpServer
from app.tools.mcp.http import HttpMcpServer
from app.tools.mcp.protocol import McpProtocolError, tool_result_text

TOOL_NAME_PREFIX = "mcp"
MAX_TOOL_OUTPUT_CHARS = 20_000


def qualified_tool_name(server: str, tool: str) -> str:
    """Namespace an external tool so it cannot shadow a built-in one.

    A server offering a tool called `bash` or `edit_file` would otherwise
    silently take over a name the policy layer has specific rules for.
    """
    return f"{TOOL_NAME_PREFIX}__{server}__{tool}"


def spec_from_mcp(server: str, descriptor: dict[str, Any]) -> ToolSpec:
    """Convert an MCP tool descriptor into a ToolSpec.

    `read_only=False` and `approval="gate"` are forced rather than read from the
    descriptor. A server declaring itself read-only is an unverifiable claim by
    third-party code, and believing it would skip the approval prompt entirely.
    """
    schema = descriptor.get("inputSchema")
    return ToolSpec(
        name=qualified_tool_name(server, str(descriptor["name"])),
        description=str(descriptor.get("description") or f"{descriptor['name']} (via MCP server {server})"),
        input_schema=schema if isinstance(schema, dict) else {"type": "object"},
        read_only=False,
        approval="gate",
    )


@dataclass(slots=True)
class McpTool:
    """A tool backed by an external MCP server."""

    spec: ToolSpec
    server: Any
    remote_name: str

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        try:
            result = await self.server.call_tool(self.remote_name, args)
        except TimeoutError:
            return ToolResult(
                success=False,
                error=f"MCP server {self.server.name!r} did not respond within its timeout",
                risk_level="medium",
                data={"mcp_server": self.server.name, "status": "timeout"},
            )
        except (McpProtocolError, OSError) as exc:
            # A failing server is reported to the model as a tool error so the
            # turn continues; it must not surface as an agent crash.
            return ToolResult(
                success=False,
                error=f"MCP server {self.server.name!r} failed: {exc}",
                risk_level="medium",
                data={"mcp_server": self.server.name, "status": "failed"},
            )
        text, is_error = tool_result_text(result)
        return ToolResult(
            success=not is_error,
            text="" if is_error else text[:MAX_TOOL_OUTPUT_CHARS],
            error=text[:MAX_TOOL_OUTPUT_CHARS] if is_error else "",
            data={"mcp_server": self.server.name},
        )


@dataclass(slots=True)
class McpServerConfig:
    """One declared server. Exactly one of `command` or `url` selects the transport."""

    name: str
    command: list[str] = field(default_factory=list)
    url: str = ""
    # Names an environment variable holding a bearer token for an HTTP server.
    # The name, never the value: a secret written into the repository's own
    # config would be committed by whoever declared the server.
    auth_token_env: str = ""
    env_allowlist: tuple[str, ...] | None = None
    startup_timeout: float = 20.0
    call_timeout: float = 60.0


@dataclass(slots=True)
class McpManager:
    """Starts configured MCP servers and exposes their tools to the registry.

    Server startup is isolated: one server failing to start leaves the others
    and the agent loop working, because an optional integration must not be able
    to take the Runtime down.
    """

    workspace: Path
    servers: list[StdioMcpServer] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)

    async def start(
        self,
        configs: list[McpServerConfig],
        *,
        on_event: Any = None,
    ) -> list[McpTool]:
        tools: list[McpTool] = []
        for config in configs:
            try:
                server = self._build(config)
            except Exception as exc:  # noqa: BLE001 - a bad URL is one server's problem
                self.failures[config.name] = f"{exc.__class__.__name__}: {exc}"
                if on_event is not None:
                    on_event(config.name, "failed", {"error": self.failures[config.name]})
                continue
            try:
                descriptors = await server.start()
            except Exception as exc:  # noqa: BLE001 - one bad server must not stop the rest
                self.failures[config.name] = f"{exc.__class__.__name__}: {exc}"
                await server.stop()
                if on_event is not None:
                    on_event(config.name, "failed", {"error": self.failures[config.name]})
                continue
            self.servers.append(server)
            server_tools = [
                McpTool(spec=spec_from_mcp(config.name, descriptor), server=server, remote_name=str(descriptor["name"]))
                for descriptor in descriptors
            ]
            tools.extend(server_tools)
            if on_event is not None:
                on_event(config.name, "started", {"tools": [tool.spec.name for tool in server_tools]})
        return tools

    def _build(self, config: McpServerConfig) -> Any:
        """Pick the transport from the config, refusing an ambiguous declaration.

        Guessing which one was meant would start something the author did not
        ask for; both or neither is an authoring mistake worth naming.
        """
        if bool(config.command) == bool(config.url):
            raise ValueError("declare exactly one of command (stdio) or url (http)")
        if config.url:
            return HttpMcpServer(
                config.name,
                config.url,
                auth_token_env=config.auth_token_env,
                startup_timeout=config.startup_timeout,
                call_timeout=config.call_timeout,
            )
        return StdioMcpServer(
            config.name,
            config.command,
            cwd=self.workspace,
            env_allowlist=config.env_allowlist,
            startup_timeout=config.startup_timeout,
            call_timeout=config.call_timeout,
        )

    async def stop(self) -> None:
        await asyncio.gather(*(server.stop() for server in self.servers), return_exceptions=True)
        self.servers.clear()

    def status(self) -> dict[str, Any]:
        return {
            "running": [server.name for server in self.servers if server.running],
            "failed": dict(self.failures),
        }
