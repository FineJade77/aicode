"""Helpers for asserting that a process group was actually killed.

These tests previously inferred liveness from a marker file written after a
sleep. That made them flaky under load and, more often, silently vacuous: if the
child had not spawned its grandchild yet when the kill fired, no marker appeared
and the assertion passed without proving anything. Observing the pid directly
removes both problems.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

# Long enough that two interpreter startups reliably complete before the kill,
# even when the whole suite is competing for CPU. The previous 0.1s made these
# tests race: the parent often had not reached Popen yet, so no grandchild
# existed and the assertion passed without proving anything — and when the fork
# landed just before the kill, the grandchild was already orphaned and survived.
GROUP_KILL_TIMEOUT = 1.0
# How long to wait for a killed process to disappear. It is reparented to init
# and reaped promptly, so this only has to absorb scheduling jitter.
GROUP_EXIT_TIMEOUT = 5.0


def grandchild_command(pid_path: Path) -> str:
    """A process that spawns a long-lived grandchild and records its pid.

    The pid goes to a file rather than stdout because stdout does not survive the
    SIGKILL path being tested. Recording the pid at all is what lets these tests
    observe the process directly instead of inferring liveness from a
    timing-based marker file — the thing that made them flaky and, more often,
    silently vacuous.
    """
    child = "import time; time.sleep(60)"
    return (
        "import pathlib, subprocess, sys, time; "
        f"process = subprocess.Popen([sys.executable, '-c', {child!r}]); "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(process.pid), encoding='utf-8'); "
        "time.sleep(60)"
    )


def grandchild_pid(pid_path: Path) -> int:
    """Read the recorded pid, failing loudly if the grandchild never started.

    Without this the tests would silently degrade into asserting nothing the
    moment the timing shifted.
    """
    assert pid_path.exists(), "the grandchild never started, so this test would prove nothing"
    recorded = pid_path.read_text(encoding="utf-8").strip()
    assert recorded.isdigit(), f"unexpected pid record: {recorded!r}"
    return int(recorded)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists but owned by another user
        return True
    return True


async def assert_process_exits(pid: int, message: str) -> None:
    deadline = asyncio.get_running_loop().time() + GROUP_EXIT_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if not process_alive(pid):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)
