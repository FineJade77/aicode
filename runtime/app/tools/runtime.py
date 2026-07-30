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

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or build_default_registry()

    def schemas_for_mode(self, mode: str) -> list[dict[str, Any]]:
        return self.registry.schemas_for_mode(mode)

    def spec_for(self, name: str) -> ToolSpec | None:
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
    ) -> ToolContext:
        return build_tool_context(
            workspace,
            mode,
            execution=execution,
            session_id=session_id,
            run_id=run_id,
            trust_level=trust_level,
        )

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> str | None:
        return self.registry.validate_arguments(name, arguments)

    async def run(self, name: str, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self.registry.run(name, arguments, context)

    def build_edit_proposal(
        self,
        workspace: Path,
        arguments: dict[str, Any],
        protected_paths: list[str],
    ) -> Any:
        return build_edit_proposal(workspace, arguments, protected_paths)

    def apply_edit(self, workspace: Path, proposal: Any) -> None:
        apply_edit(workspace, proposal)

    def is_protected_path(self, path: str, protected_paths: list[str]) -> bool:
        return is_protected_path(path, protected_paths)
