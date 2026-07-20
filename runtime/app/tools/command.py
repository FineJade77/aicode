from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def combined_output(self) -> str:
        output = self.stdout.strip()
        error = self.stderr.strip()
        if error:
            output = output + ("\n" if output else "") + error
        return output


async def run_command(command: Sequence[str], *, cwd: Path, timeout: float) -> CommandResult:
    argv = [str(part) for part in command]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    return await collect_process_result(proc, argv, timeout=timeout)


async def run_shell_command(command: str, *, cwd: Path, timeout: float, stderr_to_stdout: bool = False) -> CommandResult:
    stderr = asyncio.subprocess.STDOUT if stderr_to_stdout else asyncio.subprocess.PIPE
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
        start_new_session=True,
    )
    return await collect_process_result(proc, [command], timeout=timeout)


async def collect_process_result(proc: asyncio.subprocess.Process, command: list[str], *, timeout: float) -> CommandResult:
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        stdout, stderr = await terminate_process(proc)
        return CommandResult(
            command=command,
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=decode_output(stdout),
            stderr=decode_output(stderr) or f"命令超时: {timeout:g}s",
            timed_out=True,
        )
    except asyncio.CancelledError:
        await terminate_process(proc)
        raise

    return CommandResult(
        command=command,
        returncode=proc.returncode if proc.returncode is not None else 0,
        stdout=decode_output(stdout),
        stderr=decode_output(stderr),
    )


async def terminate_process(proc: asyncio.subprocess.Process) -> tuple[bytes | None, bytes | None]:
    if proc.returncode is None:
        killed_group = False
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            killed_group = True
        except (ProcessLookupError, PermissionError):
            pass
        if not killed_group and proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
    try:
        return await proc.communicate()
    except (RuntimeError, ValueError):
        with suppress(Exception):
            await proc.wait()
        return None, None


def decode_output(raw: bytes | None) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")
