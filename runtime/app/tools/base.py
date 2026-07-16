from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Protocol

from app.project.config import default_protected_paths


@dataclass(slots=True)
class ToolContext:
    workspace: Path
    mode: str = "default"
    language: str = "zh-CN"
    protected_paths: list[str] = field(default_factory=default_protected_paths)
    review_disabled_rules: list[str] = field(default_factory=list)
    review_large_diff_threshold: int = 500
    review_max_findings: int = 50


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


def reject_protected_path(workspace: Path, path: Path, protected_paths: list[str]) -> None:
    rel = display_path(workspace, path)
    if is_protected_path(rel, protected_paths):
        raise ToolError(f"受保护路径不可访问: {rel}")


def is_protected_path(rel_path: str, protected_paths: list[str]) -> bool:
    normalized = rel_path.replace("\\", "/").lstrip("./")
    for pattern in protected_paths:
        normalized_pattern = pattern.replace("\\", "/").lstrip("./")
        if fnmatch(normalized, normalized_pattern):
            return True
        if "/" not in normalized_pattern and Path(normalized).name == normalized_pattern:
            return True
    return False


def display_path(workspace: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace.resolve()))
    except ValueError:
        return str(path)
