import asyncio
import os
import shlex
import sys
from pathlib import Path

import pytest

from app.tools.command import run_command, run_shell_command
from tests.process_helpers import (
    GROUP_KILL_TIMEOUT,
    assert_process_exits,
    grandchild_command,
    grandchild_pid,
)


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
    assert "command timed out" in result.stderr


@pytest.mark.asyncio
async def test_run_command_timeout_kills_process_group(tmp_path: Path) -> None:
    pid_path = tmp_path / "grandchild.pid"
    result = await run_command(
        [sys.executable, "-c", grandchild_command(pid_path)], cwd=tmp_path, timeout=GROUP_KILL_TIMEOUT
    )

    assert result.timed_out
    await assert_process_exits(
        grandchild_pid(pid_path), "grandchild process leaked past the command timeout"
    )


@pytest.mark.asyncio
async def test_shell_timeout_kills_background_child_after_shell_exits(tmp_path: Path) -> None:
    """The shell exits immediately, so the background child outlives its leader
    and can only be reached through the process group."""
    pid_path = tmp_path / "background.pid"
    child = "import time; time.sleep(60)"
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(child)} & "
        f"echo $! > {shlex.quote(str(pid_path))} ; sleep 60"
    )

    result = await asyncio.wait_for(
        run_shell_command(command, cwd=tmp_path, timeout=GROUP_KILL_TIMEOUT), timeout=30
    )

    assert result.timed_out
    await assert_process_exits(
        grandchild_pid(pid_path), "background child leaked after its shell leader exited"
    )


@pytest.mark.asyncio
async def test_run_command_cancel_kills_process_group(tmp_path: Path) -> None:
    pid_path = tmp_path / "cancel.pid"
    task = asyncio.create_task(
        run_command([sys.executable, "-c", grandchild_command(pid_path)], cwd=tmp_path, timeout=30)
    )

    # Wait for the grandchild to actually exist before cancelling, rather than
    # assuming a fixed delay is long enough.
    deadline = asyncio.get_running_loop().time() + GROUP_KILL_TIMEOUT * 5
    while asyncio.get_running_loop().time() < deadline and not pid_path.exists():
        await asyncio.sleep(0.02)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await assert_process_exits(
        grandchild_pid(pid_path), "grandchild process leaked after command cancellation"
    )


@pytest.mark.asyncio
async def test_internal_argv_rejects_workspace_path_hijack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_git = tmp_path / "git"
    fake_git.write_text("#!/bin/sh\nprintf hijacked\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    with pytest.raises(ValueError, match="workspace PATH"):
        await run_command(["git", "--version"], cwd=tmp_path, timeout=5)
