from __future__ import annotations

import asyncio
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
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        stdout, stderr = await proc.communicate()
        return CommandResult(
            command=argv,
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=decode_output(stdout),
            stderr=decode_output(stderr) or f"命令超时: {timeout:g}s",
            timed_out=True,
        )

    return CommandResult(
        command=argv,
        returncode=proc.returncode if proc.returncode is not None else 0,
        stdout=decode_output(stdout),
        stderr=decode_output(stderr),
    )


def decode_output(raw: bytes | None) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")
