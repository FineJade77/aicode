"""Concrete tool runtime exposed to the agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent.ports import ToolSpec
from app.tools.base import ToolContext, ToolResult, is_protected_path
from app.tools.edit import apply_edit, build_edit_proposal
from app.tools.registry import ToolRegistry, build_default_registry, build_tool_context


class DefaultToolRuntime:
    """Adapter over the built-in workspace tools.

    Agent Core depends on this object through ToolRegistry instead of importing
    filesystem, shell, edit, or execution implementations directly. Holds a real
    registry instance, so an embedder can pass its own to add tools without
    touching Agent Core.
    """

    def __init__(self, registry: ToolRegistry | None = None, mcp: Any = None) -> None:
        self.registry = registry or build_default_registry()
        # Optional: absent means no external tools, which is the default for an
        # embedder that never configured any.
        self.mcp = mcp
        # Resolved per workspace by `prepare`. Keyed by resolved path because two
        # workspaces may declare servers of the same name with different tools,
        # and a call has to reach the server its own workspace started.
        self._mcp_tools: dict[Path, dict[str, Any]] = {}

    async def prepare(self, workspace: str, *, trust_level: str = "trusted", on_event: Any = None) -> None:
        """Start this workspace's MCP servers, if it is trusted and has any.

        Called once per run rather than per tool call: servers are subprocesses
        with a handshake, and the provider caches them across runs.
        """
        if self.mcp is None:
            return
        root = Path(workspace).expanduser().resolve()
        tools = await self.mcp.tools_for(root, trust_level=trust_level, on_event=on_event)
        self._mcp_tools[root] = {tool.spec.name: tool for tool in tools}

    def _external(self, workspace: str | Path | None) -> dict[str, Any]:
        if workspace is None:
            return {}
        return self._mcp_tools.get(Path(workspace).expanduser().resolve(), {})

    def schemas_for_mode(self, mode: str, *, workspace: str | Path | None = None) -> list[dict[str, Any]]:
        schemas = self.registry.schemas_for_mode(mode)
        # External tools are never hidden by mode: they carry no mode-specific
        # policy, and a read-only mode already refuses them at the policy layer
        # because every MCP spec declares `read_only=False`.
        schemas.extend(tool.spec.to_schema() for tool in self._external(workspace).values())
        return schemas

    def spec_for(self, name: str, *, workspace: str | Path | None = None) -> ToolSpec | None:
        external = self._external(workspace).get(name)
        if external is not None:
            return external.spec
        if workspace is None:
            # Callers without a workspace in hand (parallel-group planning) still
            # need a verdict. Any MCP tool is non-read-only and gated, so
            # returning None keeps it out of a parallel group — the conservative
            # answer, and the same one an unknown tool gets.
            for tools in self._mcp_tools.values():
                if name in tools:
                    return tools[name].spec
        return self.registry.spec_for(name)

    def specs(self) -> list[ToolSpec]:
        return self.registry.specs()

    def build_context(
        self,
        workspace: str,
        mode: str,
        *,
        execution: Any = None,
        session_id: str = "",
        run_id: str = "",
        trust_level: str = "trusted",
        session: Any = None,
        approvals: Any = None,
        bash_backend: str | None = None,
    ) -> ToolContext:
        return build_tool_context(
            workspace,
            mode,
            execution=execution,
            session_id=session_id,
            run_id=run_id,
            trust_level=trust_level,
            session=session,
            approvals=approvals,
            bash_backend=bash_backend,
        )

    def validate_arguments(
        self, name: str, arguments: dict[str, Any], *, workspace: str | Path | None = None
    ) -> str | None:
        if name in self._external(workspace):
            # The server owns its schema and validates against it. Re-checking
            # here would mean maintaining a second interpretation of a schema
            # aicode did not write.
            return None
        return self.registry.validate_arguments(name, arguments)

    async def run(self, name: str, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        external = self._external(context.workspace).get(name)
        if external is not None:
            return await external.run(arguments, context)
        return await self.registry.run(name, arguments, context)

    def build_edit_proposal(self, context: ToolContext, arguments: dict[str, Any]) -> Any:
        return build_edit_proposal(context, arguments)

    def apply_edit(self, workspace: Path, proposal: Any) -> None:
        apply_edit(workspace, proposal)

    def is_protected_path(self, path: str, protected_paths: list[str]) -> bool:
        return is_protected_path(path, protected_paths)
