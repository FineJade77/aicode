from __future__ import annotations

import shlex

from app.tools.base import ToolContext, ToolResult
from app.tools.command import run_command


class RunShellTool:
    name = "run_shell"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        timeout = int(args.get("timeout", 30))
        parts = shlex.split(command)
        if not parts:
            return ToolResult(success=False, error="空命令")

        proc = await run_command(parts, cwd=context.workspace, timeout=timeout)
        output = proc.combined_output

        return ToolResult(
            success=proc.returncode == 0,
            text=output or "命令无输出",
            error="" if proc.returncode == 0 else output or f"命令退出码: {proc.returncode}",
            data={"command": parts, "returncode": proc.returncode, "timed_out": proc.timed_out},
        )
