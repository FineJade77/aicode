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


def default_protected_paths() -> list[str]:
    return [".env", ".env.*", "secrets/**", "infra/prod/**"]


@dataclass(slots=True)
class ProjectConfig:
    project_name: str | None = None
    default_language: str | None = None
    commands: dict[str, str] = field(default_factory=dict)
    protected_paths: list[str] = field(default_factory=default_protected_paths)
    workspaces: list[WorkspaceRef] = field(default_factory=list)
    review: ReviewConfig = field(default_factory=ReviewConfig)


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

    return ProjectConfig(
        project_name=as_optional_str(raw.get("projectName")),
        default_language=as_optional_str(raw.get("defaultLanguage")),
        commands={str(key): str(value) for key, value in commands.items()} if isinstance(commands, dict) else {},
        protected_paths=[str(value) for value in protected_paths] if isinstance(protected_paths, list) else default_protected_paths(),
        workspaces=parse_workspaces(workspaces),
        review=parse_review_config(review),
    )


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
