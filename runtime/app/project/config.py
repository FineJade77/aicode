from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class WorkspaceRef:
    name: str
    path: str
    mode: str = "read_only"


@dataclass(slots=True)
class ReviewConfig:
    disabled_rules: list[str] = field(default_factory=list)
    large_diff_threshold: int = 500
    max_findings: int = 50


@dataclass(slots=True)
class ExecutionConfig:
    """Project-level override for where Agent shell commands run.

    An empty string means "inherit the Runtime-wide setting"; a project may only
    pick one of the known backends, never invent a new one.
    """

    agent_bash_backend: str = ""


def mandatory_protected_paths() -> list[str]:
    return [
        ".env",
        ".env.*",
        "**/.env",
        "**/.env.*",
        ".ssh/**",
        "**/.ssh/**",
        ".gnupg/**",
        "**/.gnupg/**",
        ".aws/**",
        "**/.aws/**",
        ".azure/**",
        "**/.azure/**",
        ".kube/**",
        "**/.kube/**",
        ".config/gcloud/**",
        "**/.config/gcloud/**",
        ".config/gh/**",
        "**/.config/gh/**",
        ".docker/**",
        "**/.docker/**",
        ".git/config",
        "**/.git/config",
        ".git-credentials",
        "**/.git-credentials",
        ".netrc",
        "**/.netrc",
        ".npmrc",
        "**/.npmrc",
        ".pypirc",
        "**/.pypirc",
        "*.pem",
        "**/*.pem",
        "*.key",
        "**/*.key",
    ]


def default_protected_paths() -> list[str]:
    return [*mandatory_protected_paths(), "secrets/**", "infra/prod/**"]


@dataclass(slots=True)
class ProjectConfig:
    project_name: str | None = None
    commands: dict[str, str] = field(default_factory=dict)
    protected_paths: list[str] = field(default_factory=default_protected_paths)
    workspaces: list[WorkspaceRef] = field(default_factory=list)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


def load_project_config(workspace: Path) -> ProjectConfig:
    config_path = workspace / ".aicode" / "config.json"
    if not config_path.exists():
        return ProjectConfig()

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ProjectConfig()

    return parse_project_config(raw)


def parse_project_config(raw: dict[str, Any]) -> ProjectConfig:
    commands = raw.get("commands")
    protected_paths = raw.get("protectedPaths")
    workspaces = raw.get("workspaces")
    review = raw.get("review")
    execution = raw.get("execution")

    return ProjectConfig(
        project_name=as_optional_str(raw.get("projectName")),
        commands={str(key): str(value) for key, value in commands.items()} if isinstance(commands, dict) else {},
        protected_paths=effective_protected_paths(protected_paths),
        workspaces=parse_workspaces(workspaces),
        review=parse_review_config(review),
        execution=parse_execution_config(execution),
    )


def parse_execution_config(raw: Any) -> ExecutionConfig:
    if not isinstance(raw, dict):
        return ExecutionConfig()
    backend = str(raw.get("agentBashBackend") or "").strip().casefold()
    # An unknown value falls back to "inherit" rather than to a permissive
    # default: a typo in project config must never silently weaken the sandbox.
    return ExecutionConfig(agent_bash_backend=backend if backend in {"auto", "host", "docker"} else "")


def parse_workspaces(raw: Any) -> list[WorkspaceRef]:
    if not isinstance(raw, list):
        return []

    refs: list[WorkspaceRef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        path = item.get("path")
        mode = item.get("mode", "read_only")
        if not name or not path:
            continue
        refs.append(WorkspaceRef(name=str(name), path=str(path), mode=str(mode)))
    return refs


def as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def parse_review_config(raw: Any) -> ReviewConfig:
    if not isinstance(raw, dict):
        return ReviewConfig()

    disabled_rules = raw.get("disabledRules")
    large_diff_threshold = bounded_int(raw.get("largeDiffThreshold"), default=500, minimum=50, maximum=50_000)
    max_findings = bounded_int(raw.get("maxFindings"), default=50, minimum=1, maximum=500)

    return ReviewConfig(
        disabled_rules=[str(value) for value in disabled_rules] if isinstance(disabled_rules, list) else [],
        large_diff_threshold=large_diff_threshold,
        max_findings=max_findings,
    )


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def effective_protected_paths(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return default_protected_paths()
    values = mandatory_protected_paths()
    for value in raw:
        text = str(value)
        if text not in values:
            values.append(text)
    return values
