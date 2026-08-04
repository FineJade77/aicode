"""Workspace-scoped MCP server lifecycle.

`McpManager` starts servers and turns them into tools; this decides *when* that
may happen and keeps the result alive for the right span.

Two properties drive the design:

- **Trust gates it.** MCP servers are declared in the repository's own
  `.aicode/config.json`, so an untrusted checkout would otherwise get to name a
  process for the daemon to launch. That is the same power `.aicode` hooks have,
  and hooks already refuse to run in an untrusted workspace; MCP follows suit
  rather than inventing a second answer to one question.
- **Servers outlive a run.** They are subprocesses with a handshake; starting
  them per turn would pay that cost on every message. They are cached per
  workspace and stopped when the Runtime shuts down.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.tools.mcp.manager import McpManager, McpServerConfig, McpTool


class McpToolProvider:
    """Starts and caches MCP servers, one set per trusted workspace."""

    def __init__(self, configs_for: Any) -> None:
        # A callable rather than a config list: which servers exist depends on
        # the workspace being opened, and reading that at construction time
        # would bind the daemon to whichever repository happened to be first.
        self.configs_for = configs_for
        self._managers: dict[Path, McpManager] = {}
        self._tools: dict[Path, list[McpTool]] = {}
        self._lock = asyncio.Lock()

    async def tools_for(
        self,
        workspace: Path,
        *,
        trust_level: str,
        on_event: Any = None,
    ) -> list[McpTool]:
        if trust_level != "trusted":
            # Silence here is deliberate: an untrusted workspace has not been
            # refused a feature it asked for, it has not been granted one.
            return []
        root = Path(workspace).expanduser().resolve()
        async with self._lock:
            if root in self._tools:
                return self._tools[root]
            configs = self.configs_for(root)
            if not configs:
                self._tools[root] = []
                return []
            manager = McpManager(workspace=root)
            tools = await manager.start(list(configs), on_event=on_event)
            self._managers[root] = manager
            self._tools[root] = tools
            return tools

    def status(self) -> dict[str, Any]:
        return {
            str(root): manager.status() for root, manager in self._managers.items()
        }

    async def aclose(self) -> None:
        managers = list(self._managers.values())
        self._managers.clear()
        self._tools.clear()
        await asyncio.gather(*(manager.stop() for manager in managers), return_exceptions=True)


def configs_from_project(load_config: Any) -> Any:
    """Adapt the project config loader into the callable the provider wants."""

    def configs_for(workspace: Path) -> list[McpServerConfig]:
        config = load_config(workspace)
        return [
            McpServerConfig(
                name=ref.name,
                command=list(ref.command),
                env_allowlist=tuple(ref.env_allowlist) or None,
                startup_timeout=ref.startup_timeout,
                call_timeout=ref.call_timeout,
            )
            for ref in getattr(config, "mcp_servers", [])
        ]

    return configs_for
