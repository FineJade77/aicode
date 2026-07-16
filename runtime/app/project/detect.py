from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.project.config import load_project_config


@dataclass(slots=True)
class ProjectInfo:
    languages: list[str] = field(default_factory=list)
    package_manager: str | None = None
    test_command: str | None = None
    config_test_command: str | None = None
    files: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_project(workspace: Path) -> ProjectInfo:
    files = {
        "go_mod": (workspace / "go.mod").exists(),
        "go_work": (workspace / "go.work").exists(),
        "package_json": (workspace / "package.json").exists(),
        "tsconfig_json": (workspace / "tsconfig.json").exists(),
        "pyproject_toml": (workspace / "pyproject.toml").exists(),
        "pytest_ini": (workspace / "pytest.ini").exists(),
        "setup_cfg": (workspace / "setup.cfg").exists(),
        "requirements_txt": (workspace / "requirements.txt").exists(),
    }

    languages: list[str] = []
    if files["go_mod"] or files["go_work"]:
        languages.append("go")
    if files["package_json"] or files["tsconfig_json"]:
        languages.append("typescript" if files["tsconfig_json"] else "javascript")
    if files["pyproject_toml"] or files["pytest_ini"] or files["setup_cfg"] or files["requirements_txt"]:
        languages.append("python")

    package_manager = detect_package_manager(workspace)
    config_test_command = configured_test_command(workspace)

    return ProjectInfo(
        languages=languages,
        package_manager=package_manager,
        test_command=detect_test_command(workspace),
        config_test_command=config_test_command,
        files=files,
    )


def detect_package_manager(workspace: Path) -> str | None:
    if (workspace / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (workspace / "yarn.lock").exists():
        return "yarn"
    if (workspace / "package-lock.json").exists() or (workspace / "package.json").exists():
        return "npm"
    if (workspace / "uv.lock").exists():
        return "uv"
    if (workspace / "poetry.lock").exists():
        return "poetry"
    if (workspace / "go.mod").exists() or (workspace / "go.work").exists():
        return "go"
    return None


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
