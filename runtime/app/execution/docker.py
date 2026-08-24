from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from app.execution.host import HostExecutionBackend
from app.execution.models import ExecutionRequest, ExecutionResult

MEMORY_PATTERN = re.compile(r"^[1-9][0-9]*(?:[kKmMgG])?$")
CPU_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


MISSING_IMAGE_MARKERS = ("unable to find image", "no such image", "image not known", "pull access denied")

# Where the artifact drop appears inside the container. Fixed rather than
# configurable: a command has to be able to name it without being told, and a
# caller-chosen path would be one more thing to validate against escaping.
ARTIFACT_MOUNT = "/artifacts"


def docker_available() -> bool:
    """Whether the Docker CLI can be located on PATH.

    Callers use this to fail loudly *before* dispatching a command that was
    routed to the sandbox. Silently falling back to host execution would turn a
    security boundary into a placebo, so a missing Docker CLI must surface as an
    error the user can act on.
    """
    return bool(shutil.which("docker"))


def missing_image_hint(command: str, output: str) -> str:
    """Turn Docker's "image not present" failure into an actionable instruction.

    Returns an empty string when the failure was not about a missing image.
    """
    if not any(marker in output.casefold() for marker in MISSING_IMAGE_MARKERS):
        return ""
    image = docker_image(command)
    return (
        f"The sandbox image {image!r} is not present locally and aicode does not pull images "
        f"implicitly. Run `docker pull {image}` once, then retry."
    )


class DockerExecutionBackend:
    def __init__(self, host: HostExecutionBackend) -> None:
        self.host = host

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if not request.shell_command:
            raise ValueError("Docker backend requires shell_command")
        if request.network != "none":
            raise ValueError("Docker backend currently supports only network=none")

        workspace = request.workspace.expanduser().resolve()
        # The only path a caller may ask to be writable is the workspace itself.
        # Anything else would put a host directory the policy layer never vetted
        # inside a container that runs model-chosen commands.
        for candidate in request.writable_paths:
            if candidate.expanduser().resolve() != workspace:
                raise ValueError("Docker backend only supports the workspace as a writable path")
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

    writable = any(candidate.expanduser().resolve() == workspace for candidate in request.writable_paths)
    artifact_root = request.artifact_root

    args = [
        "docker",
        "run",
        "--rm",
        # Never pull implicitly. A missing image would otherwise turn a single
        # tool call into a multi-minute, feedback-free image download; failing
        # immediately lets the caller surface an actionable "docker pull" hint.
        "--pull=never",
        "--network",
        "none",
        "--cpus",
        cpus,
        "--memory",
        memory,
        "--pids-limit",
        str(pids),
    ]
    if writable or artifact_root is not None:
        # Without this the container writes as root and leaves root-owned files
        # in the user's workspace on Linux. Docker Desktop remaps ownership on
        # macOS, but matching the host uid/gid is correct on both. The artifact
        # drop needs it for the same reason: files aicode cannot read afterwards
        # are not an export.
        args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    for key, value in _sandbox_environment(artifacts=artifact_root is not None).items():
        args.extend(["--env", f"{key}={value}"])
    mount = f"type=bind,src={workspace},dst=/workspace"
    args.extend(["--mount", mount if writable else f"{mount},readonly"])
    if artifact_root is not None:
        # The one writable path a read-only sandbox gets. Outside the workspace
        # by construction, so `build` can produce output without the repository
        # becoming writable to model-chosen commands.
        args.extend(["--mount", f"type=bind,src={artifact_root},dst={ARTIFACT_MOUNT}"])
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


def _sandbox_environment(*, artifacts: bool = False) -> dict[str, str]:
    environment = {
        "AICODE_SANDBOX": "1",
        "HOME": "/tmp/aicode-home",
        "XDG_CACHE_HOME": "/tmp/aicode-cache",
        "GOCACHE": "/tmp/aicode-go-build",
        "GOMODCACHE": "/tmp/aicode-go-mod",
        "npm_config_cache": "/tmp/aicode-npm-cache",
        "YARN_CACHE_FOLDER": "/tmp/aicode-yarn-cache",
        "PIP_CACHE_DIR": "/tmp/aicode-pip-cache",
    }
    if artifacts:
        # Advertised so a command can write its report without the caller having
        # to hard-code the mount path into every project's config.
        environment["AICODE_ARTIFACTS"] = ARTIFACT_MOUNT
    return environment


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
