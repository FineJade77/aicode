"""Workspace inspection exposed to the agent and application services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.project.config import ProjectConfig, load_project_config
from app.project.detect import detect_project_command, detect_test_command
from app.project.skills import discover_skills
from app.tools.registry import resolve_bash_backend
from app.tools.review import review_rules_data

BASH_ENVIRONMENT_DESCRIPTIONS = {
    "host": "commands run directly on the host with the full local toolchain and network access",
    "docker": (
        "commands run inside a Docker sandbox: no network access, only the workspace is mounted "
        "(writable), and .env files are masked. Package installs that need the network will fail"
    ),
}


@dataclass(frozen=True, slots=True)
class ProjectPromptContext:
    config: ProjectConfig
    test_command: str
    rules_text: str
    memory_text: str
    # Names and one-line descriptions only. Bodies are loaded by the `skill`
    # tool when one is chosen; putting them all here would spend the window on
    # instructions for the skills this turn is not doing.
    skills: tuple = ()
    bash_environment: str = BASH_ENVIRONMENT_DESCRIPTIONS["host"]


class LocalWorkspaceRuntime:
    def same_workspace(self, left: str, right: str) -> bool:
        if left == right:
            return True
        try:
            return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
        except OSError:
            return False

    def prompt_context(self, workspace: Path, *, trust_level: str = "trusted") -> ProjectPromptContext:
        config = load_project_config(workspace)
        configured = config.commands.get("test")
        test_command = configured if configured and configured != "auto" else (detect_test_command(workspace) or "")
        backend = resolve_bash_backend(
            config.execution.agent_bash_backend or settings.execution.agent_bash_backend,
            trust_level,
        )
        return ProjectPromptContext(
            config=config,
            test_command=test_command,
            rules_text=self._context_file(workspace, "rules.md"),
            memory_text=self._context_file(workspace, "memory.md"),
            skills=tuple(discover_skills(workspace, trust_level=trust_level)),
            bash_environment=BASH_ENVIRONMENT_DESCRIPTIONS[backend],
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
