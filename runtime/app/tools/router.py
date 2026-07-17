from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.policy.engine import PolicyDecision, PolicyEngine
from app.project.config import load_project_config
from app.tools.base import ToolContext, ToolError, ToolResult
from app.tools.file import FindFilesTool, ListFilesTool, ReadFileTool
from app.tools.git import GitDiffTool, GitShowTool, GitStatusTool
from app.tools.project import DetectProjectTool, RunTestsTool
from app.tools.review import ReviewDiffTool
from app.tools.search import SearchTextTool
from app.tools.shell import RunShellTool


class ToolRouter:
    def __init__(self, policy: PolicyEngine | None = None) -> None:
        self.policy = policy or PolicyEngine()
        self.tools = {
            tool.name: tool
            for tool in [
                ListFilesTool(),
                FindFilesTool(),
                ReadFileTool(),
                SearchTextTool(),
                DetectProjectTool(),
                GitStatusTool(),
                GitDiffTool(),
                GitShowTool(),
                ReviewDiffTool(),
                RunShellTool(),
                RunTestsTool(),
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

        return await self._run_allowed(name, args, workspace, mode, language, decision, approved=False)

    async def run_after_approval(self, name: str, args: dict[str, Any], workspace: str, mode: str, language: str) -> ToolResult:
        decision = self.policy.evaluate(name, args, mode=mode)
        if not self.is_approvable(name, decision):
            return ToolResult(
                success=False,
                error=decision.reason or "工具不支持审批后执行",
                risk_level=decision.risk_level,
                requires_approval=decision.requires_approval,
                data={"policy": asdict(decision)},
            )
        return await self._run_allowed(name, args, workspace, mode, language, decision, approved=True)

    def is_approvable(self, name: str, decision: PolicyDecision) -> bool:
        return name == "run_shell" and decision.requires_approval and decision.risk_level == "medium"

    async def _run_allowed(
        self,
        name: str,
        args: dict[str, Any],
        workspace: str,
        mode: str,
        language: str,
        decision: PolicyDecision,
        approved: bool,
    ) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(success=False, error=f"未知工具: {name}", risk_level="high")

        project_config = load_project_config(Path(workspace))
        try:
            result = await tool.run(
                args,
                ToolContext(
                    workspace=Path(workspace),
                    mode=mode,
                    language=language,
                    protected_paths=project_config.protected_paths,
                    workspace_refs=project_config.workspaces,
                    review_disabled_rules=project_config.review.disabled_rules,
                    review_large_diff_threshold=project_config.review.large_diff_threshold,
                    review_max_findings=project_config.review.max_findings,
                ),
            )
        except ToolError as exc:
            return ToolResult(success=False, error=str(exc), risk_level=decision.risk_level, requires_approval=decision.requires_approval)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc), risk_level="high", requires_approval=False)
        if result.risk_level == "low":
            result.risk_level = decision.risk_level
        result.requires_approval = result.requires_approval or decision.requires_approval
        if approved:
            result.data["approved"] = True
        return result

    def evaluate(self, name: str, args: dict[str, Any], mode: str) -> PolicyDecision:
        return self.policy.evaluate(name, args, mode=mode)
