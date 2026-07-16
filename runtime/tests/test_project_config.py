import json
from pathlib import Path

import pytest

from app.project.config import load_project_config, parse_project_config
from app.project.detect import detect_project, detect_test_command
from app.tools.patch import create_append_patch
from app.tools.router import ToolRouter


def test_parse_project_config() -> None:
    config = parse_project_config(
        {
            "projectName": "demo",
            "defaultLanguage": "en-US",
            "commands": {"test": "python3 -m pytest tests/unit"},
            "protectedPaths": [".env", "secret/**"],
            "workspaces": [{"name": "api", "path": "../api", "mode": "read_only"}],
            "review": {
                "disabledRules": ["large_diff"],
                "largeDiffThreshold": 1200,
                "maxFindings": 25,
            },
        }
    )

    assert config.project_name == "demo"
    assert config.default_language == "en-US"
    assert config.commands["test"] == "python3 -m pytest tests/unit"
    assert config.protected_paths == [".env", "secret/**"]
    assert config.workspaces[0].name == "api"
    assert config.review.disabled_rules == ["large_diff"]
    assert config.review.large_diff_threshold == 1200
    assert config.review.max_findings == 25


def test_load_project_config_defaults_when_missing(tmp_path: Path) -> None:
    config = load_project_config(tmp_path)

    assert ".env" in config.protected_paths
    assert config.commands == {}
    assert config.review.disabled_rules == []
    assert config.review.large_diff_threshold == 500
    assert config.review.max_findings == 50


def test_parse_project_config_bounds_review_values() -> None:
    config = parse_project_config({"review": {"largeDiffThreshold": 1, "maxFindings": 1000}})

    assert config.review.large_diff_threshold == 50
    assert config.review.max_findings == 500


def test_detect_test_command_uses_project_config_override(tmp_path: Path) -> None:
    write_project_config(tmp_path, {"commands": {"test": "python3 -m pytest tests/unit"}})
    (tmp_path / "go.work").write_text("go 1.22\n\nuse ./cli\n", encoding="utf-8")

    assert detect_test_command(tmp_path) == "python3 -m pytest tests/unit"


def test_detect_project_for_go_work(tmp_path: Path) -> None:
    (tmp_path / "go.work").write_text("go 1.22\n\nuse ./cli\n", encoding="utf-8")

    info = detect_project(tmp_path)

    assert info.languages == ["go"]
    assert info.package_manager == "go"
    assert info.test_command == "go test ./cli/..."


@pytest.mark.asyncio
async def test_read_file_blocks_project_protected_path(tmp_path: Path) -> None:
    write_project_config(tmp_path, {"protectedPaths": ["secret/**"]})
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "token.txt").write_text("token", encoding="utf-8")

    result = await ToolRouter().run("read_file", {"path": "secret/token.txt"}, str(tmp_path), "default", "zh-CN")

    assert not result.success
    assert "受保护路径" in result.error


@pytest.mark.asyncio
async def test_list_files_hides_project_protected_path(tmp_path: Path) -> None:
    write_project_config(tmp_path, {"protectedPaths": ["secret/**"]})
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "token.txt").write_text("token", encoding="utf-8")
    (tmp_path / "public.txt").write_text("ok", encoding="utf-8")

    result = await ToolRouter().run("list_files", {"max_depth": 2}, str(tmp_path), "default", "zh-CN")

    assert result.success
    assert "public.txt" in result.text
    assert "secret/token.txt" not in result.text


@pytest.mark.asyncio
async def test_search_text_skips_project_protected_path(tmp_path: Path) -> None:
    write_project_config(tmp_path, {"protectedPaths": ["secret/**"]})
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "token.txt").write_text("needle", encoding="utf-8")

    result = await ToolRouter().run("search_text", {"query": "needle"}, str(tmp_path), "default", "zh-CN")

    assert result.success
    assert "token.txt" not in result.text


def test_append_patch_blocks_project_protected_path(tmp_path: Path) -> None:
    protected = tmp_path / "secret.txt"
    protected.write_text("secret\n", encoding="utf-8")

    with pytest.raises(Exception, match="受保护路径"):
        create_append_patch(tmp_path, "secret.txt", "new", protected_paths=["secret.txt"])


@pytest.mark.asyncio
async def test_detect_project_tool(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module demo\n", encoding="utf-8")

    result = await ToolRouter().run("detect_project", {}, str(tmp_path), "default", "zh-CN")

    assert result.success
    assert result.data["languages"] == ["go"]
    assert result.data["test_command"] == "go test ./..."


@pytest.mark.asyncio
async def test_run_tests_tool_uses_safe_configured_command(tmp_path: Path) -> None:
    write_project_config(tmp_path, {"commands": {"test": "python3 -m unittest discover"}})

    result = await ToolRouter().run("run_tests", {"timeout": 30}, str(tmp_path), "default", "zh-CN")

    assert result.success
    assert result.data["command"] == ["python3", "-m", "unittest", "discover"]


@pytest.mark.asyncio
async def test_run_tests_tool_blocks_dangerous_configured_command(tmp_path: Path) -> None:
    write_project_config(tmp_path, {"commands": {"test": "rm -rf build"}})

    result = await ToolRouter().run("run_tests", {"timeout": 30}, str(tmp_path), "default", "zh-CN")

    assert not result.success
    assert result.risk_level == "high"


def write_project_config(tmp_path: Path, data: dict) -> None:
    config_dir = tmp_path / ".aicode"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")
