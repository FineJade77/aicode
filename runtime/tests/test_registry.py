import pytest

from app.tools.base import ToolContext
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
async def test_unknown_tool(tmp_path):
    result = await run_tool("mystery", {}, make_context(tmp_path))
    assert not result.success
