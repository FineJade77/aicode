from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from app.execution import ExecutionRequest, ExecutionService, ResourceLimits


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    execution_id: str = ""
    backend: str = "host"
    status: str = ""
    duration_ms: int = 0

    @property
    def combined_output(self) -> str:
        output = self.stdout.strip()
        error = self.stderr.strip()
        if error:
            output += ("\n" if output else "") + error
        return output


async def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    execution: ExecutionService | None = None,
    metadata: dict[str, Any] | None = None,
) -> CommandResult:
    argv = resolve_internal_argv(tuple(str(part) for part in command), cwd)
    request = _request(
        cwd=cwd,
        timeout=timeout,
        argv=argv,
        execution=execution,
        metadata=metadata,
    )
    result = await (execution or ExecutionService()).execute(request)
    return _command_result(list(argv), result)


def resolve_internal_argv(argv: tuple[str, ...], cwd: Path) -> tuple[str, ...]:
    executable = argv[0]
    if "/" in executable or "\\" in executable:
        return argv
    resolved_text = shutil.which(executable)
    if not resolved_text:
        return argv
    resolved = Path(resolved_text).resolve()
    try:
        resolved.relative_to(cwd.resolve())
    except ValueError:
        return (str(resolved), *argv[1:])
    raise ValueError(f"拒绝执行 workspace PATH 中的内部工具: {executable}")


async def run_shell_command(
    command: str,
    *,
    cwd: Path,
    timeout: float,
    stderr_to_stdout: bool = False,
    execution: ExecutionService | None = None,
    metadata: dict[str, Any] | None = None,
) -> CommandResult:
    request = _request(
        cwd=cwd,
        timeout=timeout,
        shell_command=command,
        merge_stderr=stderr_to_stdout,
        execution=execution,
        metadata=metadata,
    )
    result = await (execution or ExecutionService()).execute(request)
    return _command_result([command], result)


def _request(
    *,
    cwd: Path,
    timeout: float,
    argv: tuple[str, ...] | None = None,
    shell_command: str | None = None,
    merge_stderr: bool = False,
    execution: ExecutionService | None = None,
    metadata: dict[str, Any] | None = None,
) -> ExecutionRequest:
    del execution
    metadata = metadata or {}
    return ExecutionRequest(
        workspace=cwd,
        argv=argv,
        shell_command=shell_command,
        merge_stderr=merge_stderr,
        action=str(metadata.get("action") or ""),
        mode=str(metadata.get("mode") or "default"),
        session_id=str(metadata.get("session_id") or ""),
        run_id=str(metadata.get("run_id") or ""),
        tool_call_id=str(metadata.get("tool_call_id") or ""),
        allowed_roots=(cwd.expanduser().resolve(),),
        masked_paths=tuple(str(value) for value in metadata.get("masked_paths") or ()),
        trust_level=str(metadata.get("trust_level") or "unspecified"),
        limits=ResourceLimits(timeout_seconds=timeout),
    )


def _command_result(command: list[str], result) -> CommandResult:
    return CommandResult(
        command=command,
        returncode=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        timed_out=result.timed_out,
        cancelled=result.cancelled,
        execution_id=result.execution_id,
        backend=result.backend,
        status=result.status.value,
        duration_ms=result.duration_ms,
    )
