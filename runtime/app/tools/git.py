from __future__ import annotations

import subprocess

from app.tools.base import ToolContext, ToolResult


class GitStatusTool:
    name = "git_status"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        return run_git(context, ["status", "--short"])


class GitDiffTool:
    name = "git_diff"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        extra = ["--", str(args["path"])] if args.get("path") else []
        return run_git(context, ["diff", *extra], empty_text="当前没有未提交 diff")


class GitShowTool:
    name = "git_show"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        ref = str(args.get("ref", "HEAD"))
        return run_git(context, ["show", "--stat", "--oneline", ref])


def run_git(context: ToolContext, args: list[str], empty_text: str = "无输出") -> ToolResult:
    proc = subprocess.run(
        ["git", *args],
        cwd=context.workspace,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    output = proc.stdout.strip()
    error = proc.stderr.strip()
    if proc.returncode != 0:
        return ToolResult(success=False, error=error or output or "git 命令失败")
    return ToolResult(success=True, text=output or empty_text, data={"command": ["git", *args]})
