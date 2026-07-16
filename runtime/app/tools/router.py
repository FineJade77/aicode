from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.policy.engine import PolicyDecision, PolicyEngine
from app.tools.base import ToolContext, ToolError, ToolResult
from app.tools.file import ListFilesTool, ReadFileTool
from app.tools.git import GitDiffTool, GitShowTool, GitStatusTool
from app.tools.search import SearchTextTool
from app.tools.shell import RunShellTool


class ToolRouter:
    def __init__(self, policy: PolicyEngine | None = None) -> None:
        self.policy = policy or PolicyEngine()
        self.tools = {
            tool.name: tool
            for tool in [
                ListFilesTool(),
                ReadFileTool(),
                SearchTextTool(),
                GitStatusTool(),
                GitDiffTool(),
                GitShowTool(),
                RunShellTool(),
            ]
        }

    async def run(self, name: str, args: dict[str, Any], workspace: str, mode: str, language: str) -> ToolResult:
        decision = self.policy.evaluate(name, args, mode=mode)
        if not decision.allowed:
            return ToolResult(
                success=False,
                error=decision.reason or "工具调用被策略拦截",
                risk_level=decision.risk_level,
                requires_approval=decision.requires_approval,
                data={"policy": asdict(decision)},
            )

        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(success=False, error=f"未知工具: {name}", risk_level="high")

        try:
            result = await tool.run(args, ToolContext(workspace=Path(workspace), mode=mode, language=language))
        except ToolError as exc:
            return ToolResult(success=False, error=str(exc), risk_level=decision.risk_level, requires_approval=decision.requires_approval)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc), risk_level="high", requires_approval=False)
        result.risk_level = decision.risk_level
        result.requires_approval = decision.requires_approval
        return result

    def evaluate(self, name: str, args: dict[str, Any], mode: str) -> PolicyDecision:
        return self.policy.evaluate(name, args, mode=mode)
