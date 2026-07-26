from __future__ import annotations

from pathlib import Path

from app.tools.base import (
    IGNORED_DIRS,
    ToolContext,
    ToolResult,
    display_path,
    is_within_workspace,
    is_protected_path,
    resolve_tool_workspace,
    resolve_workspace_path,
    scoped_display_path,
)


class ListFilesTool:
    name = "list_files"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        workspace_root, workspace_name = resolve_tool_workspace(context, args.get("workspace"))
        root = resolve_workspace_path(workspace_root, args.get("path"))
        max_depth = int(args.get("max_depth", 1))
        limit = int(args.get("limit", 80))

        if not root.exists():
            return ToolResult(success=False, error=f"路径不存在: {root}")
        if not root.is_dir():
            return ToolResult(success=False, error=f"不是目录: {root}")

        files: list[str] = []
        walk(root, workspace_root, workspace_name, files, max_depth=max_depth, limit=limit, protected_paths=context.protected_paths)
        text = "\n".join(f"- {item}" for item in files) if files else "未发现文件"
        return ToolResult(success=True, text=text, data={"files": files, "workspace": workspace_name or "main"})


def walk(
    root: Path,
    workspace: Path,
    workspace_name: str,
    out: list[str],
    max_depth: int,
    limit: int,
    protected_paths: list[str],
    depth: int = 0,
) -> None:
    if len(out) >= limit:
        return
    if depth > max_depth:
        return

    for child in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if len(out) >= limit:
            return
        if not is_within_workspace(workspace, child):
            continue
        if child.name in IGNORED_DIRS:
            continue
        rel = display_path(workspace, child)
        if is_protected_path(rel, protected_paths):
            continue
        suffix = "/" if child.is_dir() else ""
        out.append(scoped_display_path(workspace_name, workspace, child) + suffix)
        if child.is_dir() and not child.is_symlink() and depth < max_depth:
            walk(
                child,
                workspace,
                workspace_name,
                out,
                max_depth=max_depth,
                limit=limit,
                protected_paths=protected_paths,
                depth=depth + 1,
            )
