from __future__ import annotations

from app.tools.base import ToolContext, ToolResult
from app.tools.command import run_command


class GitStatusTool:
    name = "git_status"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        return await run_git(context, ["status", "--short"])


class GitDiffTool:
    name = "git_diff"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        extra = ["--", str(args["path"])] if args.get("path") else []
        return await run_git(context, ["diff", *extra], empty_text="当前没有未提交 diff")


class GitShowTool:
    name = "git_show"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        ref = str(args.get("ref", "HEAD"))
        return await run_git(context, ["show", "--stat", "--oneline", ref])


async def run_git(context: ToolContext, args: list[str], empty_text: str = "无输出") -> ToolResult:
    command = ["git", *args]
    proc = await run_command(command, cwd=context.workspace, timeout=20)
    output = proc.stdout.strip()
    error = proc.stderr.strip()
    if proc.returncode != 0:
        return ToolResult(success=False, error=error or output or "git 命令失败")
    return ToolResult(success=True, text=output or empty_text, data={"command": command, "timed_out": proc.timed_out})
