from __future__ import annotations

import shlex
import subprocess

from app.policy.engine import PolicyEngine
from app.project.detect import detect_project
from app.tools.base import ToolContext, ToolResult


class DetectProjectTool:
    name = "detect_project"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        info = detect_project(context.workspace)
        lines = [
            "项目识别:",
            f"- languages: {', '.join(info.languages) if info.languages else 'unknown'}",
            f"- package_manager: {info.package_manager or 'unknown'}",
            f"- test_command: {info.test_command or 'not detected'}",
        ]
        return ToolResult(success=True, text="\n".join(lines), data=info.to_dict())


class RunTestsTool:
    name = "run_tests"

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        info = detect_project(context.workspace)
        command = str(args.get("command") or info.test_command or "").strip()
        if not command:
            return ToolResult(success=False, error="未发现可运行的测试命令", data=info.to_dict())

        decision = PolicyEngine().evaluate("run_shell", {"command": command}, mode=context.mode)
        if not decision.allowed:
            return ToolResult(
                success=False,
                error=decision.reason,
                risk_level=decision.risk_level,
                requires_approval=decision.requires_approval,
                data={"project": info.to_dict(), "command": command},
            )

        timeout = int(args.get("timeout", 120))
        parts = shlex.split(command)
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
            text=output or "测试命令无输出",
            error="" if proc.returncode == 0 else output or f"测试命令退出码: {proc.returncode}",
            data={"project": info.to_dict(), "command": parts, "returncode": proc.returncode},
            risk_level=decision.risk_level,
            requires_approval=decision.requires_approval,
        )
