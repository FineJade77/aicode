from pathlib import Path

import pytest

from app.tools.router import ToolRouter


@pytest.mark.asyncio
async def test_list_files_reads_workspace(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    result = await ToolRouter().run("list_files", {"path": ".", "max_depth": 2}, str(tmp_path), "default", "zh-CN")

    assert result.success
    assert "src/" in result.text
    assert "src/main.py" in result.text


@pytest.mark.asyncio
async def test_read_file_blocks_path_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    result = await ToolRouter().run("read_file", {"path": "../outside.txt"}, str(tmp_path), "default", "zh-CN")

    assert not result.success
    assert "workspace" in result.error


@pytest.mark.asyncio
async def test_run_shell_policy_blocks_rm(tmp_path: Path) -> None:
    result = await ToolRouter().run("run_shell", {"command": "rm -rf build"}, str(tmp_path), "default", "zh-CN")

    assert not result.success
    assert result.risk_level == "high"


@pytest.mark.asyncio
async def test_run_shell_after_approval_executes_medium_command(tmp_path: Path) -> None:
    result = await ToolRouter().run_after_approval(
        "run_shell",
        {"command": "python3 -c 'print(123)'"},
        str(tmp_path),
        "default",
        "zh-CN",
    )

    assert result.success
    assert result.risk_level == "medium"
    assert result.requires_approval
    assert result.data["approved"] is True
    assert "123" in result.text


@pytest.mark.asyncio
async def test_run_shell_after_approval_still_blocks_high_risk_command(tmp_path: Path) -> None:
    result = await ToolRouter().run_after_approval(
        "run_shell",
        {"command": "rm -rf build"},
        str(tmp_path),
        "default",
        "zh-CN",
    )

    assert not result.success
    assert result.risk_level == "high"
