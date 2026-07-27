from __future__ import annotations

from pathlib import Path
from typing import Any

from app.tools.base import is_protected_path
from app.tools.edit import apply_edit, build_edit_proposal
from app.tools.registry import build_tool_context, run_tool, tool_schemas_for_mode, validate_tool_arguments


class DefaultToolRuntime:
    """Adapter over the built-in workspace tools.

    Agent Core depends on this object through ToolRuntime instead of importing
    filesystem, shell, edit, or execution implementations directly.
    """

    def schemas_for_mode(self, mode: str) -> list[dict[str, Any]]:
        return tool_schemas_for_mode(mode)

    def build_context(
        self,
        workspace: str,
        mode: str,
        language: str,
        *,
        execution: Any = None,
        session_id: str = "",
        run_id: str = "",
        trust_level: str = "trusted",
    ) -> Any:
        return build_tool_context(
            workspace,
            mode,
            language,
            execution=execution,
            session_id=session_id,
            run_id=run_id,
            trust_level=trust_level,
        )

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> str | None:
        return validate_tool_arguments(name, arguments)

    async def run(self, name: str, arguments: dict[str, Any], context: Any) -> Any:
        return await run_tool(name, arguments, context)

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
