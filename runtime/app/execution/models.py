from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    timeout_seconds: float = 120.0
    cpus: str | None = None
    memory: str | None = None
    pids_limit: int | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.timeout_seconds > 3600:
            raise ValueError("timeout_seconds 必须在 0-3600 秒之间")
        if self.pids_limit is not None and not 1 <= self.pids_limit <= 65535:
            raise ValueError("pids_limit 必须在 1-65535 之间")


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    workspace: Path
    argv: tuple[str, ...] | None = None
    shell_command: str | None = None
    backend: str = "host"
    execution_id: str = field(default_factory=lambda: f"exec_{uuid.uuid4().hex}")
    action: str = ""
    mode: str = "default"
    session_id: str = ""
    run_id: str = ""
    tool_call_id: str = ""
    allowed_roots: tuple[Path, ...] = ()
    writable_paths: tuple[Path, ...] = ()
    masked_paths: tuple[str, ...] = ()
    env_allowlist: tuple[str, ...] | None = None
    trust_level: str = "unspecified"
    network: str = "inherit"
    merge_stderr: bool = False
    image: str = ""
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def __post_init__(self) -> None:
        has_argv = bool(self.argv)
        has_shell = bool(self.shell_command and self.shell_command.strip())
        if has_argv == has_shell:
            raise ValueError("ExecutionRequest 必须且只能设置 argv 或 shell_command")
        if self.backend not in {"host", "docker"}:
            raise ValueError(f"不支持的 execution backend: {self.backend}")
        if not self.execution_id.strip():
            raise ValueError("execution_id 不能为空")

    @property
    def executable(self) -> str:
        if self.argv:
            return self.argv[0]
        return "shell"

    def command_payload(self) -> bytes:
        if self.argv:
            return json.dumps(list(self.argv), ensure_ascii=False, separators=(",", ":")).encode()
        return (self.shell_command or "").encode()


@dataclass(slots=True)
class ExecutionResult:
    execution_id: str
    backend: str
    status: ExecutionStatus
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    cancelled: bool = False

    @property
    def success(self) -> bool:
        return self.status == ExecutionStatus.SUCCEEDED

    @property
    def combined_output(self) -> str:
        output = self.stdout.strip()
        error = self.stderr.strip()
        if error:
            output += ("\n" if output else "") + error
        return output

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "backend": self.backend,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
        }
