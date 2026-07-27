from __future__ import annotations

import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path

from app.execution.host import HostExecutionBackend
from app.execution.models import ExecutionRequest, ExecutionResult


MEMORY_PATTERN = re.compile(r"^[1-9][0-9]*(?:[kKmMgG])?$")
CPU_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


class DockerExecutionBackend:
    def __init__(self, host: HostExecutionBackend) -> None:
        self.host = host

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if not request.shell_command:
            raise ValueError("Docker backend requires shell_command")
        if request.network != "none":
            raise ValueError("Docker backend currently supports only network=none")
        if request.writable_paths:
            raise ValueError("Docker backend currently supports only a read-only workspace")

        workspace = request.workspace.expanduser().resolve()
        with tempfile.TemporaryDirectory(prefix="aicode-sandbox-mask-") as temp_dir:
            mask_source = Path(temp_dir) / "empty"
            mask_source.write_bytes(b"")
            masks = _env_file_masks(workspace, mask_source)
            docker_request = replace(
                request,
                argv=tuple(_docker_args(request, workspace, masks)),
                shell_command=None,
                env_allowlist=(
                    "PATH",
                    "HOME",
                    "DOCKER_HOST",
                    "DOCKER_CONTEXT",
                    "DOCKER_CONFIG",
                    "XDG_CONFIG_HOME",
                ),
            )
            return await self.host.execute(docker_request)

    async def cancel(self, execution_id: str) -> bool:
        return await self.host.cancel(execution_id)

    async def cancel_all(self) -> None:
        await self.host.cancel_all()


def _docker_args(request: ExecutionRequest, workspace: Path, masks: list[tuple[Path, str]]) -> list[str]:
    limits = request.limits
    cpus = limits.cpus or "2"
    memory = limits.memory or "2g"
    pids = limits.pids_limit or 256
    if not CPU_PATTERN.fullmatch(cpus) or float(cpus) <= 0:
        raise ValueError("Docker cpus limit is invalid")
    if not MEMORY_PATTERN.fullmatch(memory):
        raise ValueError("Docker memory limit is invalid")

    args = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cpus",
        cpus,
        "--memory",
        memory,
        "--pids-limit",
        str(pids),
    ]
    for key, value in _sandbox_environment().items():
        args.extend(["--env", f"{key}={value}"])
    args.extend(["--mount", f"type=bind,src={workspace},dst=/workspace,readonly"])
    for source, target in masks:
        args.extend(["--mount", f"type=bind,src={source},dst={target},readonly"])
    args.extend(
        [
            "-w",
            "/workspace",
            request.image or docker_image(request.shell_command or ""),
            "sh",
            "-lc",
            request.shell_command or "",
        ]
    )
    return args


def _sandbox_environment() -> dict[str, str]:
    return {
        "AICODE_SANDBOX": "1",
        "HOME": "/tmp/aicode-home",
        "XDG_CACHE_HOME": "/tmp/aicode-cache",
        "GOCACHE": "/tmp/aicode-go-build",
        "GOMODCACHE": "/tmp/aicode-go-mod",
        "npm_config_cache": "/tmp/aicode-npm-cache",
        "YARN_CACHE_FOLDER": "/tmp/aicode-yarn-cache",
        "PIP_CACHE_DIR": "/tmp/aicode-pip-cache",
    }


def _env_file_masks(workspace: Path, empty_file: Path) -> list[tuple[Path, str]]:
    masks: list[tuple[Path, str]] = []
    for candidate in sorted(workspace.glob(".env*")):
        if candidate.is_file() and not candidate.is_symlink():
            masks.append((empty_file, f"/workspace/{candidate.name}"))
    return masks


def docker_image(command: str) -> str:
    override = os.getenv("AICODE_SANDBOX_DOCKER_IMAGE", "").strip()
    if override:
        return override
    executable = command.strip().split(maxsplit=1)[0] if command.strip() else ""
    if executable == "go":
        return "golang:1.22"
    if executable in {"node", "npm", "pnpm", "yarn"}:
        return "node:22"
    if executable in {"python", "python3", "pytest"}:
        return "python:3.12-slim"
    return "ubuntu:24.04"
