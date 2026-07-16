from pathlib import Path

import pytest

from app.tools.command import run_command


@pytest.mark.asyncio
async def test_run_command_captures_output(tmp_path: Path) -> None:
    result = await run_command(["python3", "-c", "print('hello')"], cwd=tmp_path, timeout=5)

    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr == ""
    assert not result.timed_out


@pytest.mark.asyncio
async def test_run_command_reports_timeout(tmp_path: Path) -> None:
    result = await run_command(["python3", "-c", "import time; time.sleep(1)"], cwd=tmp_path, timeout=0.05)

    assert result.returncode != 0
    assert result.timed_out
    assert "命令超时" in result.stderr
