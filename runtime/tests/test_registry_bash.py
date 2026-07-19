import asyncio

import pytest

from app.tools.base import ToolContext
from app.tools.registry import run_tool


@pytest.mark.asyncio
async def test_bash_runs_command(tmp_path):
    (tmp_path / "hello.txt").write_text("x", encoding="utf-8")
    result = await run_tool("bash", {"command": "ls"}, ToolContext(workspace=tmp_path))
    assert result.success
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
    assert "超时" in result.error


@pytest.mark.asyncio
async def test_bash_timeout_kills_grandchild_process(tmp_path):
    # 复现问题：sh -c "<command>" 派生出的子进程（如这里 (...) 子 shell 中的 sleep）
    # 是 run_bash 启动进程的孙子进程。若超时只 kill 直接子进程，孙子进程会被
    # reparent 后继续跑完。用一个会在短暂延迟后创建标记文件的孙子进程来验证：
    # 若进程组被整体杀死，标记文件不应该出现；若发生泄漏，标记文件会按时出现。
    marker = tmp_path / "grandchild_marker"
    command = f"echo start && (sleep 2 && touch {marker})"
    result = await run_tool("bash", {"command": command, "timeout": 1}, ToolContext(workspace=tmp_path))
    assert not result.success
    assert "超时" in result.error

    await asyncio.sleep(2.5)
    assert not marker.exists(), "grandchild process leaked past the tool timeout"
