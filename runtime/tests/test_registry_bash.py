import asyncio

import pytest

from app.tools.base import ToolContext
from app.tools.registry import run_tool


@pytest.mark.asyncio
async def test_bash_runs_command(tmp_path):
    (tmp_path / "hello.txt").write_text("x", encoding="utf-8")
    result = await run_tool("bash", {"command": "ls"}, ToolContext(workspace=tmp_path))
    assert result.success
    assert isinstance(result.duration_ms, int)
    assert result.text.startswith("exit=0")
    assert "hello.txt" in result.text


@pytest.mark.asyncio
async def test_bash_nonzero_exit(tmp_path):
    result = await run_tool("bash", {"command": "false"}, ToolContext(workspace=tmp_path))
    assert not result.success
    assert "exit=1" in result.text


@pytest.mark.asyncio
async def test_bash_timeout(tmp_path):
    result = await run_tool("bash", {"command": "sleep 5", "timeout": 1}, ToolContext(workspace=tmp_path))
    assert not result.success
    assert "timed out" in result.error
    assert result.data["timed_out"] is True
    assert isinstance(result.duration_ms, int)


@pytest.mark.asyncio
async def test_bash_timeout_kills_grandchild_process(tmp_path):
    # A subprocess spawned by sh -c, such as the sleep in this subshell, is a
    # grandchild of run_bash. Killing only the direct child on timeout lets the
    # reparented grandchild finish. The delayed marker proves that the whole
    # process group is killed: the marker must never appear.
    marker = tmp_path / "grandchild_marker"
    command = f"echo start && (sleep 2 && touch {marker})"
    result = await run_tool("bash", {"command": command, "timeout": 1}, ToolContext(workspace=tmp_path))
    assert not result.success
    assert "timed out" in result.error

    await asyncio.sleep(2.5)
    assert not marker.exists(), "grandchild process leaked past the tool timeout"


@pytest.mark.asyncio
async def test_bash_does_not_inherit_provider_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AICODE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    result = await run_tool(
        "bash",
        {"command": "printf %s \"$OPENAI_API_KEY\""},
        ToolContext(workspace=tmp_path, trust_level="trusted"),
    )

    assert result.success
    assert "must-not-leak" not in result.text
