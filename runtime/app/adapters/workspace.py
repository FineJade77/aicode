from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.project.config import ProjectConfig, load_project_config
from app.project.detect import detect_project_command, detect_test_command
from app.tools.review import review_rules_data


@dataclass(frozen=True, slots=True)
class ProjectPromptContext:
    config: ProjectConfig
    test_command: str
    rules_text: str
    memory_text: str


class LocalWorkspaceRuntime:
    def same_workspace(self, left: str, right: str) -> bool:
        if left == right:
            return True
        try:
            return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
        except OSError:
            return False

    def prompt_context(self, workspace: Path) -> ProjectPromptContext:
        config = load_project_config(workspace)
        configured = config.commands.get("test")
        test_command = configured if configured and configured != "auto" else (detect_test_command(workspace) or "")
        return ProjectPromptContext(
            config=config,
            test_command=test_command,
            rules_text=self._context_file(workspace, "rules.md"),
            memory_text=self._context_file(workspace, "memory.md"),
        )

    def project_command(self, workspace: Path, action: str) -> str | None:
        return detect_project_command(workspace, action)

    def review_rules(self, workspace: str | None = None) -> dict:
        if workspace is None:
            return review_rules_data()
        config = load_project_config(Path(workspace))
        return review_rules_data(
            disabled_rules=config.review.disabled_rules,
            large_diff_threshold=config.review.large_diff_threshold,
            max_findings=config.review.max_findings,
        )

    @staticmethod
    def _context_file(workspace: Path, filename: str) -> str:
        path = workspace / ".aicode" / filename
        if not path.is_file():
            return ""
        return path.read_text("utf-8", errors="replace")[:4000]
