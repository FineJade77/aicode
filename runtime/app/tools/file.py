from __future__ import annotations

from pathlib import Path

from app.tools.base import IGNORED_DIRS, ToolContext, ToolError, ToolResult, display_path, reject_protected_path, resolve_workspace_path


class ListFilesTool:
    name = "list_files"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        root = resolve_workspace_path(context.workspace, args.get("path"))
        max_depth = int(args.get("max_depth", 1))
        limit = int(args.get("limit", 80))

        if not root.exists():
            return ToolResult(success=False, error=f"路径不存在: {root}")
        if not root.is_dir():
            return ToolResult(success=False, error=f"不是目录: {root}")

        files: list[str] = []
        walk(root, context.workspace, files, max_depth=max_depth, limit=limit)
        text = "\n".join(f"- {item}" for item in files) if files else "未发现文件"
        return ToolResult(success=True, text=text, data={"files": files})


class ReadFileTool:
    name = "read_file"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        path = resolve_workspace_path(context.workspace, str(args.get("path", "")))
        reject_protected_path(path)

        if not path.exists():
            return ToolResult(success=False, error=f"文件不存在: {display_path(context.workspace, path)}")
        if not path.is_file():
            return ToolResult(success=False, error=f"不是文件: {display_path(context.workspace, path)}")

        max_bytes = int(args.get("max_bytes", 40_000))
        content = path.read_bytes()[:max_bytes]
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(success=False, error=f"文件不是 UTF-8 文本: {display_path(context.workspace, path)}")

        truncated = path.stat().st_size > max_bytes
        header = f"# {display_path(context.workspace, path)}"
        if truncated:
            header += f" (truncated to {max_bytes} bytes)"
        return ToolResult(
            success=True,
            text=header + "\n" + text,
            data={"path": display_path(context.workspace, path), "truncated": truncated},
        )


def walk(root: Path, workspace: Path, out: list[str], max_depth: int, limit: int, depth: int = 0) -> None:
    if len(out) >= limit:
        return
    if depth > max_depth:
        return

    for child in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if len(out) >= limit:
            return
        if child.name in IGNORED_DIRS:
            continue
        suffix = "/" if child.is_dir() else ""
        out.append(display_path(workspace, child) + suffix)
        if child.is_dir() and depth < max_depth:
            walk(child, workspace, out, max_depth=max_depth, limit=limit, depth=depth + 1)
