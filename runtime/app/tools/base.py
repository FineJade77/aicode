from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(slots=True)
class ToolContext:
    workspace: Path
    mode: str = "default"
    language: str = "zh-CN"


@dataclass(slots=True)
class ToolResult:
    success: bool
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    risk_level: str = "low"
    requires_approval: bool = False


class Tool(Protocol):
    name: str

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        ...


class ToolError(Exception):
    pass


PROTECTED_NAMES = {".env", ".env.local", ".env.production"}
IGNORED_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}


def resolve_workspace_path(workspace: Path, raw_path: str | None = None) -> Path:
    workspace = workspace.resolve()
    if raw_path in {None, "", "."}:
        return workspace

    candidate = (workspace / raw_path).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise ToolError("路径越过 workspace 边界") from exc
    return candidate


def reject_protected_path(path: Path) -> None:
    if path.name in PROTECTED_NAMES:
        raise ToolError(f"受保护文件不可读取: {path.name}")


def display_path(workspace: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace.resolve()))
    except ValueError:
        return str(path)
