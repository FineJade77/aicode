from __future__ import annotations

import json
from pathlib import Path

from app.project.config import load_project_config


def detect_test_command(workspace: Path) -> str | None:
    configured = configured_test_command(workspace)
    if configured:
        return configured

    if (workspace / "go.mod").exists():
        return "go test ./..."

    go_work = workspace / "go.work"
    if go_work.exists():
        modules = parse_go_work_modules(go_work)
        if modules:
            packages = " ".join(f"{module}/..." for module in modules)
            return f"go test {packages}"

    if (workspace / "pyproject.toml").exists() or (workspace / "pytest.ini").exists() or (workspace / "setup.cfg").exists():
        return "python3 -m pytest"

    if (workspace / "package.json").exists():
        return detect_package_test_command(workspace)

    return None


def configured_test_command(workspace: Path) -> str | None:
    configured = load_project_config(workspace).commands.get("test")
    if configured and configured != "auto":
        return configured
    return None


def detect_package_test_command(workspace: Path) -> str | None:
    package_json = workspace / "package.json"
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        package = {}

    scripts = package.get("scripts") if isinstance(package, dict) else None
    if isinstance(scripts, dict) and "test" not in scripts:
        return None

    if (workspace / "pnpm-lock.yaml").exists():
        return "pnpm test"
    if (workspace / "yarn.lock").exists():
        return "yarn test"
    return "npm test"


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
