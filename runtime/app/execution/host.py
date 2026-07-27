from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from app.execution.models import ExecutionRequest, ExecutionResult, ExecutionStatus
from app.security.secrets import sensitive_env_key


PROCESS_DRAIN_TIMEOUT_SECONDS = 1.0
DEFAULT_HOST_ENV_KEYS = {
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "TERM",
    "COLORTERM",
    "NO_COLOR",
    "FORCE_COLOR",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "CI",
    "GOROOT",
    "JAVA_HOME",
    "SDKROOT",
}


class HostExecutionBackend:
    def __init__(self) -> None:
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.perf_counter()
        workspace = request.workspace.expanduser().resolve()
        if not workspace.is_dir():
            return self._failure(request, started, f"workspace does not exist or is not a directory: {workspace}")
        if request.allowed_roots and not any(_is_within(workspace, root) for root in request.allowed_roots):
            return self._failure(request, started, "workspace is outside allowed_roots")

        env = build_subprocess_environment(request.env_allowlist, workspace=workspace)

        stderr_target = asyncio.subprocess.STDOUT if request.merge_stderr else asyncio.subprocess.PIPE
        try:
            if request.argv:
                process = await asyncio.create_subprocess_exec(
                    *request.argv,
                    cwd=str(workspace),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr_target,
                    start_new_session=True,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    request.shell_command or "",
                    cwd=str(workspace),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr_target,
                    start_new_session=True,
                )
        except OSError as exc:
            return self._failure(request, started, f"{exc.__class__.__name__}: {exc}")

        self._processes[request.execution_id] = process
        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=request.limits.timeout_seconds,
                )
            except TimeoutError:
                stdout, stderr = await terminate_process(process)
                return ExecutionResult(
                    execution_id=request.execution_id,
                    backend=request.backend,
                    status=ExecutionStatus.TIMED_OUT,
                    exit_code=process.returncode if process.returncode is not None else -1,
                    stdout=decode_output(stdout),
                    stderr=decode_output(stderr) or f"command timed out: {request.limits.timeout_seconds:g}s",
                    duration_ms=_elapsed_ms(started),
                    timed_out=True,
                )
            except asyncio.CancelledError:
                self._cancelled.add(request.execution_id)
                await terminate_process(process)
                raise

            cancelled = request.execution_id in self._cancelled
            returncode = process.returncode if process.returncode is not None else -1
            status = ExecutionStatus.CANCELLED if cancelled else (
                ExecutionStatus.SUCCEEDED if returncode == 0 else ExecutionStatus.FAILED
            )
            return ExecutionResult(
                execution_id=request.execution_id,
                backend=request.backend,
                status=status,
                exit_code=returncode,
                stdout=decode_output(stdout),
                stderr=decode_output(stderr),
                duration_ms=_elapsed_ms(started),
                cancelled=cancelled,
            )
        finally:
            self._processes.pop(request.execution_id, None)
            self._cancelled.discard(request.execution_id)

    async def cancel(self, execution_id: str) -> bool:
        process = self._processes.get(execution_id)
        if process is None:
            return False
        self._cancelled.add(execution_id)
        kill_process_group(process)
        return True

    async def cancel_all(self) -> None:
        for execution_id in list(self._processes):
            await self.cancel(execution_id)

    @staticmethod
    def _failure(request: ExecutionRequest, started: float, message: str) -> ExecutionResult:
        return ExecutionResult(
            execution_id=request.execution_id,
            backend=request.backend,
            status=ExecutionStatus.FAILED,
            exit_code=-1,
            stderr=message,
            duration_ms=_elapsed_ms(started),
        )


async def terminate_process(process: asyncio.subprocess.Process) -> tuple[bytes | None, bytes | None]:
    kill_process_group(process)
    try:
        return await asyncio.wait_for(process.communicate(), timeout=PROCESS_DRAIN_TIMEOUT_SECONDS)
    except (TimeoutError, RuntimeError, ValueError):
        with suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=PROCESS_DRAIN_TIMEOUT_SECONDS)
        return None, None


def kill_process_group(process: asyncio.subprocess.Process) -> None:
    killed_group = False
    try:
        os.killpg(process.pid, signal.SIGKILL)
        killed_group = True
    except (ProcessLookupError, PermissionError):
        pass
    if not killed_group and process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()


def decode_output(raw: bytes | None) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


def build_subprocess_environment(
    allowlist: tuple[str, ...] | None,
    *,
    workspace: Path | None = None,
) -> dict[str, str]:
    requested = set(allowlist) if allowlist is not None else DEFAULT_HOST_ENV_KEYS
    environment = {
        key: value
        for key, value in os.environ.items()
        if (key in requested or (allowlist is None and key.startswith("LC_"))) and not sensitive_env_key(key)
    }
    if allowlist is None:
        execution_home = isolated_execution_home(workspace)
        execution_tmp = execution_home / "tmp"
        execution_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(execution_tmp, 0o700)
        environment["HOME"] = str(execution_home)
        environment["XDG_CONFIG_HOME"] = str(execution_home / ".config")
        environment["XDG_CACHE_HOME"] = str(execution_home / ".cache")
        environment["TMPDIR"] = str(execution_tmp)
        environment["TMP"] = str(execution_tmp)
        environment["TEMP"] = str(execution_tmp)
    return environment


def isolated_execution_home(workspace: Path | None = None) -> Path:
    configured = os.getenv("AICODE_EXECUTION_HOME", "").strip()
    if configured:
        base = Path(configured).expanduser()
    else:
        aicode_home = os.getenv("AICODE_HOME", "").strip()
        base = Path(aicode_home).expanduser() / "execution-home" if aicode_home else (
            Path(tempfile.gettempdir()) / f"aicode-execution-{os.getuid()}"
        )
    home = base
    if workspace is not None:
        workspace_hash = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]
        home = base / workspace_hash
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    return home.resolve()


def _is_within(path, root) -> bool:
    try:
        path.relative_to(root.expanduser().resolve())
        return True
    except ValueError:
        return False


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
