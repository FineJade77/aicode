from __future__ import annotations

import json
from pathlib import Path

from app.project.config import load_project_config


def detect_test_command(workspace: Path) -> str | None:
    return detect_project_command(workspace, "test")


def detect_project_command(workspace: Path, action: str) -> str | None:
    if action not in {"test", "build", "lint"}:
        raise ValueError(f"不支持的 project command action: {action}")

    configured = configured_project_command(workspace, action)
    if configured:
        return configured

    if (workspace / "package.json").exists():
        package_command = detect_package_script_command(workspace, action)
        if package_command:
            return package_command

    if (workspace / "go.mod").exists():
        return go_command(action, "./...")

    go_work = workspace / "go.work"
    if go_work.exists():
        modules = parse_go_work_modules(go_work)
        if modules:
            packages = " ".join(f"{module}/..." for module in modules)
            return go_command(action, packages)

    if action == "test" and (
        (workspace / "pyproject.toml").exists()
        or (workspace / "pytest.ini").exists()
        or (workspace / "setup.cfg").exists()
    ):
        return "python3 -m pytest"
    if action == "lint" and has_ruff_config(workspace):
        return "python3 -m ruff check ."

    return None


def configured_test_command(workspace: Path) -> str | None:
    return configured_project_command(workspace, "test")


def configured_project_command(workspace: Path, action: str) -> str | None:
    configured = load_project_config(workspace).commands.get(action)
    if configured and configured != "auto":
        return configured
    return None


def detect_package_test_command(workspace: Path) -> str | None:
    return detect_package_script_command(workspace, "test")


def detect_package_script_command(workspace: Path, action: str) -> str | None:
    package_json = workspace / "package.json"
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        package = {}

    scripts = package.get("scripts") if isinstance(package, dict) else None
    if isinstance(scripts, dict) and action not in scripts:
        return None

    if (workspace / "pnpm-lock.yaml").exists():
        return f"pnpm {action}"
    if (workspace / "yarn.lock").exists():
        return f"yarn {action}"
    return "npm test" if action == "test" else f"npm run {action}"


def go_command(action: str, packages: str) -> str:
    verb = {"test": "test", "build": "build", "lint": "vet"}[action]
    return f"go {verb} {packages}"


def has_ruff_config(workspace: Path) -> bool:
    if (workspace / "ruff.toml").exists() or (workspace / ".ruff.toml").exists():
        return True
    try:
        return "[tool.ruff" in (workspace / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return False


def parse_go_work_modules(go_work: Path) -> list[str]:
    modules: list[str] = []
    in_use_block = False
    for raw in go_work.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line == "use (":
            in_use_block = True
            continue
        if in_use_block and line == ")":
            in_use_block = False
            continue
        if line.startswith("use "):
            modules.append(line.removeprefix("use ").strip())
            continue
        if in_use_block:
            modules.append(line)
    return [module for module in modules if module.startswith("./")]
