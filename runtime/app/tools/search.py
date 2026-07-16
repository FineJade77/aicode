from __future__ import annotations

import shutil

from pathlib import Path

from app.tools.base import IGNORED_DIRS, ToolContext, ToolResult, display_path, is_protected_path, resolve_workspace_path
from app.tools.command import run_command


class SearchTextTool:
    name = "search_text"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(success=False, error="搜索词不能为空")

        root = resolve_workspace_path(context.workspace, args.get("path"))
        limit = int(args.get("limit", 80))

        if shutil.which("rg"):
            return await run_rg(context, root, query, limit)
        return run_python_search(context, root, query, limit)


async def run_rg(context: ToolContext, root, query: str, limit: int) -> ToolResult:
    command = [
        "rg",
        "--line-number",
        "--hidden",
        "--glob",
        "!.git",
        "--glob",
        "!node_modules",
        "--glob",
        "!.venv",
        "--glob",
        "!__pycache__",
    ]
    for pattern in context.protected_paths:
        command.extend(["--glob", "!" + pattern])
    command.extend([query, str(root)])
    proc = await run_command(command, cwd=context.workspace, timeout=15)
    if proc.returncode not in {0, 1}:
        return ToolResult(success=False, error=proc.stderr.strip() or "rg 执行失败")

    filtered_lines = filter_protected_rg_lines(context, proc.stdout.splitlines())
    lines = filtered_lines[:limit]
    text = "\n".join(lines) if lines else "未找到匹配"
    return ToolResult(success=True, text=text, data={"matches": lines, "truncated": len(filtered_lines) > limit})


def filter_protected_rg_lines(context: ToolContext, lines: list[str]) -> list[str]:
    filtered: list[str] = []
    for line in lines:
        raw_path = line.split(":", 1)[0]
        rel = display_path(context.workspace, Path(raw_path))
        if is_protected_path(rel, context.protected_paths):
            continue
        filtered.append(line)
    return filtered


def run_python_search(context: ToolContext, root, query: str, limit: int) -> ToolResult:
    matches: list[str] = []
    for path in root.rglob("*"):
        if len(matches) >= limit:
            break
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        if is_protected_path(display_path(context.workspace, path), context.protected_paths):
            continue
        try:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if query in line:
                    matches.append(f"{display_path(context.workspace, path)}:{line_number}:{line.strip()}")
                    if len(matches) >= limit:
                        break
        except UnicodeDecodeError:
            continue
    text = "\n".join(matches) if matches else "未找到匹配"
    return ToolResult(success=True, text=text, data={"matches": matches, "truncated": len(matches) >= limit})
