from __future__ import annotations

from pathlib import Path
from fnmatch import fnmatch

from app.tools.base import (
    IGNORED_DIRS,
    ToolContext,
    ToolError,
    ToolResult,
    display_path,
    is_protected_path,
    reject_protected_path,
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


class ReadFileTool:
    name = "read_file"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        workspace_root, workspace_name = resolve_tool_workspace(context, args.get("workspace"))
        path = resolve_workspace_path(workspace_root, str(args.get("path", "")))
        reject_protected_path(workspace_root, path, context.protected_paths)

        if not path.exists():
            return ToolResult(success=False, error=f"文件不存在: {scoped_display_path(workspace_name, workspace_root, path)}")
        if not path.is_file():
            return ToolResult(success=False, error=f"不是文件: {scoped_display_path(workspace_name, workspace_root, path)}")

        max_bytes = int(args.get("max_bytes", 40_000))
        content = path.read_bytes()[:max_bytes]
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(success=False, error=f"文件不是 UTF-8 文本: {scoped_display_path(workspace_name, workspace_root, path)}")

        truncated = path.stat().st_size > max_bytes
        display = scoped_display_path(workspace_name, workspace_root, path)
        header = f"# {display}"
        if truncated:
            header += f" (truncated to {max_bytes} bytes)"
        return ToolResult(
            success=True,
            text=header + "\n" + text,
            data={"path": display, "workspace": workspace_name or "main", "truncated": truncated},
        )


class FindFilesTool:
    name = "find_files"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(success=False, error="文件查询不能为空")

        workspace_root, workspace_name = resolve_tool_workspace(context, args.get("workspace"))
        root = resolve_workspace_path(workspace_root, args.get("path"))
        limit = int(args.get("limit", 40))
        if not root.exists():
            return ToolResult(success=False, error=f"路径不存在: {scoped_display_path(workspace_name, workspace_root, root)}")
        if not root.is_dir():
            return ToolResult(success=False, error=f"不是目录: {scoped_display_path(workspace_name, workspace_root, root)}")

        files = find_files(root, workspace_root, workspace_name, query, limit=limit, protected_paths=context.protected_paths)
        visible = files[:limit]
        text = "\n".join(f"- {item}" for item in visible) if visible else "未找到文件"
        return ToolResult(
            success=True,
            text=text,
            data={
                "files": visible,
                "query": query,
                "workspace": workspace_name or "main",
                "truncated": len(files) > limit,
            },
        )


def find_files(root: Path, workspace: Path, workspace_name: str, query: str, limit: int, protected_paths: list[str]) -> list[str]:
    matches: list[str] = []
    normalized_query = query.lower().replace("\\", "/")
    has_glob = any(char in query for char in "*?[]")

    for path in root.rglob("*"):
        if len(matches) > limit:
            break
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        rel = display_path(workspace, path).replace("\\", "/")
        if is_protected_path(rel, protected_paths):
            continue
        rel_lower = rel.lower()
        name_lower = path.name.lower()
        if has_glob:
            matched = fnmatch(rel_lower, normalized_query) or fnmatch(name_lower, normalized_query)
        else:
            matched = normalized_query in rel_lower or normalized_query in name_lower
        if matched:
            matches.append(scoped_display_path(workspace_name, workspace, path))
    return matches


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
        if child.name in IGNORED_DIRS:
            continue
        rel = display_path(workspace, child)
        if is_protected_path(rel, protected_paths):
            continue
        suffix = "/" if child.is_dir() else ""
        out.append(scoped_display_path(workspace_name, workspace, child) + suffix)
        if child.is_dir() and depth < max_depth:
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
