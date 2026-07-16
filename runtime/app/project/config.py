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


def default_protected_paths() -> list[str]:
    return [".env", ".env.*", "secrets/**", "infra/prod/**"]


@dataclass(slots=True)
class ProjectConfig:
    project_name: str | None = None
    default_language: str | None = None
    commands: dict[str, str] = field(default_factory=dict)
    protected_paths: list[str] = field(default_factory=default_protected_paths)
    workspaces: list[WorkspaceRef] = field(default_factory=list)


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

    return ProjectConfig(
        project_name=as_optional_str(raw.get("projectName")),
        default_language=as_optional_str(raw.get("defaultLanguage")),
        commands={str(key): str(value) for key, value in commands.items()} if isinstance(commands, dict) else {},
        protected_paths=[str(value) for value in protected_paths] if isinstance(protected_paths, list) else default_protected_paths(),
        workspaces=parse_workspaces(workspaces),
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
