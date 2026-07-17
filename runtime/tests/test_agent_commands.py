import json
from pathlib import Path
from types import SimpleNamespace

from app.agent.commands import choose_context_tools


def test_choose_context_tools_reads_workspace_scoped_file(tmp_path: Path) -> None:
    write_project_config(tmp_path, {"workspaces": [{"name": "api", "path": "../api", "mode": "read_only"}]})
    request = request_for(tmp_path, "解释 api:src/service.py")

    tools = choose_context_tools(request)

    assert tools == [("read_file", {"path": "src/service.py", "max_bytes": 30_000, "workspace": "api"})]


def test_choose_context_tools_strips_line_suffix_from_workspace_file(tmp_path: Path) -> None:
    write_project_config(tmp_path, {"workspaces": [{"name": "api", "path": "../api", "mode": "read_only"}]})
    request = request_for(tmp_path, "解释 api:src/service.py:12")

    tools = choose_context_tools(request)

    assert tools == [("read_file", {"path": "src/service.py", "max_bytes": 30_000, "workspace": "api"})]


def test_choose_context_tools_routes_workspace_scoped_diff(tmp_path: Path) -> None:
    write_project_config(tmp_path, {"workspaces": [{"name": "api", "path": "../api", "mode": "read_only"}]})
    request = request_for(tmp_path, "查看 api:src/service.py diff")

    tools = choose_context_tools(request)

    assert tools == [("git_diff", {"workspace": "api", "path": "src/service.py"})]


def test_choose_context_tools_routes_workspace_search(tmp_path: Path) -> None:
    write_project_config(tmp_path, {"workspaces": [{"name": "api", "path": "../api", "mode": "read_only"}]})
    request = request_for(tmp_path, "在 api 搜索 login 相关代码")

    tools = choose_context_tools(request)

    assert tools == [("search_text", {"query": "login", "limit": 40, "workspace": "api"})]


def test_choose_context_tools_reads_existing_root_file(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    request = request_for(tmp_path, "解释 README.md")

    tools = choose_context_tools(request)

    assert tools == [("read_file", {"path": "README.md", "max_bytes": 30_000})]


def test_choose_context_tools_finds_unknown_file_name(tmp_path: Path) -> None:
    request = request_for(tmp_path, "修复 user_service.py 的登录逻辑")

    tools = choose_context_tools(request)

    assert tools[0] == ("find_files", {"query": "user_service.py", "limit": 20})
    assert ("search_text", {"query": "登录", "limit": 40}) in tools


def test_choose_context_tools_reads_multiple_explicit_paths(tmp_path: Path) -> None:
    request = request_for(tmp_path, "修复 src/calc.py tests/test_calc.py")

    tools = choose_context_tools(request)

    assert tools[:2] == [
        ("read_file", {"path": "src/calc.py", "max_bytes": 30_000}),
        ("read_file", {"path": "tests/test_calc.py", "max_bytes": 30_000}),
    ]


def request_for(workspace: Path, message: str):
    return SimpleNamespace(message=message, mode="chat", workspace=str(workspace), language="zh-CN")


def write_project_config(workspace: Path, data: dict) -> None:
    config_dir = workspace / ".aicode"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")
