from __future__ import annotations

import shlex
import subprocess

from app.tools.base import ToolContext, ToolResult


class RunShellTool:
    name = "run_shell"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        timeout = int(args.get("timeout", 30))
        parts = shlex.split(command)
        if not parts:
            return ToolResult(success=False, error="空命令")

        proc = subprocess.run(
            parts,
            cwd=context.workspace,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        output = stdout
        if stderr:
            output = output + ("\n" if output else "") + stderr

        return ToolResult(
            success=proc.returncode == 0,
            text=output or "命令无输出",
            error="" if proc.returncode == 0 else output or f"命令退出码: {proc.returncode}",
            data={"command": parts, "returncode": proc.returncode},
        )
