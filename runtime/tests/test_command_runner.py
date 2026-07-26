import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from app.tools.command import run_command, run_shell_command


def grandchild_marker_command(marker: Path) -> str:
    child = f"import pathlib, time; time.sleep(0.8); pathlib.Path({str(marker)!r}).write_text('leaked', encoding='utf-8')"
    return f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(2)"


@pytest.mark.asyncio
async def test_run_command_captures_output(tmp_path: Path) -> None:
    result = await run_command([sys.executable, "-c", "print('hello')"], cwd=tmp_path, timeout=5)

    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr == ""
    assert not result.timed_out


@pytest.mark.asyncio
async def test_run_command_reports_timeout(tmp_path: Path) -> None:
    result = await run_command([sys.executable, "-c", "import time; time.sleep(1)"], cwd=tmp_path, timeout=0.05)

    assert result.returncode != 0
    assert result.timed_out
    assert "命令超时" in result.stderr


@pytest.mark.asyncio
async def test_run_command_timeout_kills_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild_marker"
    result = await run_command([sys.executable, "-c", grandchild_marker_command(marker)], cwd=tmp_path, timeout=0.1)

    assert result.timed_out
    await asyncio.sleep(1)
    assert not marker.exists(), "grandchild process leaked past the command timeout"


@pytest.mark.asyncio
async def test_shell_timeout_kills_background_child_after_shell_exits(tmp_path: Path) -> None:
    marker = tmp_path / "background_marker"
    child = f"import pathlib, time; time.sleep(0.8); pathlib.Path({str(marker)!r}).write_text('leaked', encoding='utf-8')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(child)} &"

    result = await asyncio.wait_for(run_shell_command(command, cwd=tmp_path, timeout=0.05), timeout=2)

    assert result.timed_out
    await asyncio.sleep(1)
    assert not marker.exists(), "background child leaked after its shell leader exited"


@pytest.mark.asyncio
async def test_run_command_cancel_kills_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "cancel_marker"
    task = asyncio.create_task(run_command([sys.executable, "-c", grandchild_marker_command(marker)], cwd=tmp_path, timeout=5))

    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(1)
    assert not marker.exists(), "grandchild process leaked after command cancellation"
