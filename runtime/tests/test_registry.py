import pytest
from pathlib import Path

from app.project.config import WorkspaceRef
from app.tools.base import ToolContext
from app.tools.command import CommandResult
from app.tools.registry import TOOL_SCHEMAS, run_tool, tool_schemas_for_mode


def make_context(tmp_path) -> ToolContext:
    return ToolContext(workspace=tmp_path)


def test_schema_names_and_modes():
    names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert names == {"read_file", "search", "list_files", "bash", "edit_file", "review_diff"}
    review_names = {schema["name"] for schema in tool_schemas_for_mode("review")}
    assert review_names == {"read_file", "search", "list_files", "review_diff"}
    for schema in TOOL_SCHEMAS:
        assert schema["description"]
        assert schema["input_schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_read_file_returns_numbered_lines(tmp_path):
    (tmp_path / "a.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    result = await run_tool("read_file", {"path": "a.py", "offset": 2, "limit": 1}, make_context(tmp_path))
    assert result.success
    assert "2\tline2" in result.text
    assert "line1" not in result.text
    assert "共 3 行" in result.text


@pytest.mark.asyncio
async def test_read_file_missing(tmp_path):
    result = await run_tool("read_file", {"path": "nope.py"}, make_context(tmp_path))
    assert not result.success
    assert "不存在" in result.error


@pytest.mark.asyncio
async def test_run_tool_validates_required_arguments(tmp_path):
    result = await run_tool("read_file", {}, make_context(tmp_path))
    assert not result.success
    assert "参数校验失败" in result.error
    assert result.data["validation_error"] == "缺少必填字段: path"


@pytest.mark.asyncio
async def test_run_tool_validates_argument_types(tmp_path):
    (tmp_path / "a.py").write_text("line1\n", encoding="utf-8")
    result = await run_tool("read_file", {"path": "a.py", "offset": "2"}, make_context(tmp_path))
    assert not result.success
    assert result.data["validation_error"] == "offset 应为 integer"


@pytest.mark.asyncio
async def test_read_file_protected(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    result = await run_tool("read_file", {"path": ".env"}, make_context(tmp_path))
    assert not result.success


@pytest.mark.asyncio
async def test_search_finds_matches(tmp_path):
    (tmp_path / "a.py").write_text("def login():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("login docs\n", encoding="utf-8")
    result = await run_tool("search", {"query": "login", "glob": "*.py"}, make_context(tmp_path))
    assert result.success
    assert "a.py" in result.text
    assert "b.md" not in result.text


@pytest.mark.asyncio
async def test_search_excludes_protected_path(tmp_path):
    (tmp_path / ".env").write_text("SECRET=login\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("def login():\n    pass\n", encoding="utf-8")
    result = await run_tool("search", {"query": "login"}, make_context(tmp_path))
    assert result.success
    assert "app.py" in result.text
    assert ".env" not in result.text
    assert "SECRET" not in result.text


@pytest.mark.asyncio
async def test_read_file_offset_out_of_range(tmp_path):
    (tmp_path / "a.py").write_text("line1\nline2\n", encoding="utf-8")
    result = await run_tool("read_file", {"path": "a.py", "offset": 99}, make_context(tmp_path))
    assert not result.success
    assert "超出" in result.error


@pytest.mark.asyncio
async def test_search_no_match(tmp_path):
    (tmp_path / "a.py").write_text("nothing here\n", encoding="utf-8")
    result = await run_tool("search", {"query": "zzz_not_found"}, make_context(tmp_path))
    assert result.success
    assert "没有匹配" in result.text


@pytest.mark.asyncio
async def test_search_reports_rg_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("app.tools.registry.shutil.which", lambda name: "/fake/rg")

    async def fake_run_command(command, *, cwd, timeout):
        return CommandResult(command=list(command), returncode=-9, stderr="命令超时: 30s", timed_out=True)

    monkeypatch.setattr("app.tools.registry.run_command", fake_run_command)
    result = await run_tool("search", {"query": "needle"}, make_context(tmp_path))

    assert not result.success
    assert "搜索超时" in result.error


@pytest.mark.asyncio
async def test_unknown_tool(tmp_path):
    result = await run_tool("mystery", {}, make_context(tmp_path))
    assert not result.success


@pytest.mark.asyncio
async def test_read_file_reads_configured_read_only_workspace(tmp_path):
    main = tmp_path / "main"
    api = tmp_path / "api"
    main.mkdir()
    (api / "src").mkdir(parents=True)
    (api / "src" / "service.py").write_text("def helper():\n    return 'ok'\n", encoding="utf-8")

    context = ToolContext(
        workspace=main,
        workspace_refs=[WorkspaceRef(name="api", path=str(api), mode="read_only")],
    )
    result = await run_tool("read_file", {"path": "src/service.py", "workspace": "api"}, context)

    assert result.success
    assert "src/service.py" in result.text
    assert "api:" in result.text
    assert "return 'ok'" in result.text


@pytest.mark.asyncio
async def test_list_files_lists_configured_read_only_workspace(tmp_path):
    main = tmp_path / "main"
    api = tmp_path / "api"
    main.mkdir()
    (api / "src").mkdir(parents=True)
    (api / "src" / "service.py").write_text("print('ok')\n", encoding="utf-8")

    context = ToolContext(
        workspace=main,
        workspace_refs=[WorkspaceRef(name="api", path=str(api), mode="read_only")],
    )
    result = await run_tool("list_files", {"workspace": "api", "max_depth": 2}, context)

    assert result.success
    assert "api:" in result.text
    assert "src" in result.text
    assert "service.py" in result.text


@pytest.mark.asyncio
async def test_search_searches_configured_read_only_workspace(tmp_path):
    main = tmp_path / "main"
    api = tmp_path / "api"
    main.mkdir()
    api.mkdir()
    (api / "public.py").write_text("needle = 'visible'\n", encoding="utf-8")
    (api / "secret").mkdir()
    (api / "secret" / "token.txt").write_text("needle = 'hidden'\n", encoding="utf-8")

    context = ToolContext(
        workspace=main,
        protected_paths=["secret/**"],
        workspace_refs=[WorkspaceRef(name="api", path=str(api), mode="read_only")],
    )
    result = await run_tool("search", {"query": "needle", "workspace": "api"}, context)

    assert result.success
    assert "api:" in result.text
    assert "public.py" in result.text
    assert "token.txt" not in result.text


@pytest.mark.asyncio
async def test_read_file_blocks_path_escape_from_configured_workspace(tmp_path):
    main = tmp_path / "main"
    api = tmp_path / "api"
    main.mkdir()
    api.mkdir()
    (main / "secret.txt").write_text("secret\n", encoding="utf-8")

    context = ToolContext(
        workspace=main,
        workspace_refs=[WorkspaceRef(name="api", path=str(api), mode="read_only")],
    )
    result = await run_tool("read_file", {"path": "../main/secret.txt", "workspace": "api"}, context)

    assert not result.success
    assert "workspace 边界" in result.error or "路径越过" in result.error


@pytest.mark.asyncio
async def test_unknown_workspace_rejected(tmp_path):
    main = tmp_path / "main"
    main.mkdir()

    context = ToolContext(
        workspace=main,
        workspace_refs=[WorkspaceRef(name="api", path=str(tmp_path / "api"), mode="read_only")],
    )
    result = await run_tool("read_file", {"path": "x.py", "workspace": "nope"}, context)

    assert not result.success
    assert "未知 workspace" in result.error
