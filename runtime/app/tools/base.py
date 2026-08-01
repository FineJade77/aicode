from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.agent.ports import ToolSpec
from app.project.config import WorkspaceRef, default_protected_paths
from app.security import is_protected_path


@dataclass(slots=True)
class ToolContext:
    workspace: Path
    mode: str = "default"
    protected_paths: list[str] = field(default_factory=default_protected_paths)
    workspace_refs: list[WorkspaceRef] = field(default_factory=list)
    review_disabled_rules: list[str] = field(default_factory=list)
    review_large_diff_threshold: int = 500
    review_max_findings: int = 50
    execution: Any = None
    session_id: str = ""
    # The live session, for tools whose effect *is* session state (update_plan).
    # Typed as Any so the tool layer does not import the session implementation;
    # only the AgentSession protocol surface is used.
    session: Any = None
    # The approval broker, for tools whose effect is a round trip to the user
    # (ask_user). Same reason as `session` for being typed Any.
    approvals: Any = None
    run_id: str = ""
    tool_call_id: str = ""
    trust_level: str = "trusted"
    # "auto" | "host" | "docker" | "os" — resolved against trust_level at call time.
    bash_backend: str = "auto"
    # Project-declared commands attached to tool events. Typed loosely for the
    # same reason as `session`: the tool layer does not import the project config
    # model just to carry it.
    hooks: list[Any] = field(default_factory=list)

    def resolved_bash_backend(self) -> str:
        """The concrete backend this context's shell commands land on.

        Hooks run wherever Agent commands run, so a sandboxed workspace does not
        gain a host-executing side channel by declaring one.
        """
        from app.tools.registry import resolve_bash_backend

        return resolve_bash_backend(self.bash_backend, self.trust_level)


@dataclass(slots=True)
class ToolResult:
    success: bool
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    risk_level: str = "low"
    requires_approval: bool = False
    duration_ms: int = 0


class Tool(Protocol):
    """A registered tool: its declaration plus how to run it."""

    spec: ToolSpec

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult: ...


class ToolError(Exception):
    pass


IGNORED_DIRS = {".git", ".aicode", ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}


def resolve_workspace_path(workspace: Path, raw_path: str | None = None) -> Path:
    workspace = workspace.resolve()
    if raw_path in {None, "", "."}:
        return workspace

    candidate = (workspace / raw_path).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise ToolError("path escapes the workspace boundary") from exc
    return candidate


def is_within_workspace(workspace: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(workspace.resolve())
        return True
    except (OSError, ValueError):
        return False


def resolve_tool_workspace(context: ToolContext, raw_workspace: Any = None) -> tuple[Path, str]:
    name = str(raw_workspace or "").strip()
    if name in {"", ".", "main", "primary"}:
        return context.workspace.resolve(), ""

    for ref in context.workspace_refs:
        if ref.name != name:
            continue
        if ref.mode != "read_only":
            raise ToolError(f"workspace supports only read_only mode: {name}")
        root = Path(ref.path).expanduser()
        if not root.is_absolute():
            root = context.workspace / root
        return root.resolve(), name

    raise ToolError(f"unknown workspace: {name}")


def scoped_display_path(workspace_name: str, workspace: Path, path: Path) -> str:
    rel = display_path(workspace, path)
    if not workspace_name:
        return rel
    return f"{workspace_name}:{rel}"


def reject_protected_path(workspace: Path, path: Path, protected_paths: list[str]) -> None:
    rel = display_path(workspace, path)
    if is_protected_path(rel, protected_paths):
        raise ToolError(f"protected path is not accessible: {rel}")


def display_path(workspace: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace.resolve()))
    except ValueError:
        return str(path)
