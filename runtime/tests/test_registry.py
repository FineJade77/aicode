import pytest

from app.agent.policy import PolicyEngine
from app.agent.ports import ToolSpec
from app.project.config import WorkspaceRef
from app.tools.base import ToolContext, ToolResult
from app.tools.command import CommandResult
from app.tools.registry import (
    DEFAULT_REGISTRY,
    TOOL_SCHEMAS,
    WRITE_HIDDEN_MODES,
    build_default_registry,
    run_tool,
    tool_schemas_for_mode,
)


def make_context(tmp_path) -> ToolContext:
    return ToolContext(workspace=tmp_path)


def test_schema_names_and_modes():
    names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert names == {
        "read_file", "search", "glob", "list_files", "related_files",
        "bash", "edit_file", "update_plan", "ask_user", "review_diff",
        "read_output", "stop_command",
    }
    review_names = {schema["name"] for schema in tool_schemas_for_mode("review")}
    assert review_names == {"read_file", "search", "glob", "list_files", "related_files", "review_diff"}
    explain_names = {schema["name"] for schema in tool_schemas_for_mode("explain")}
    assert explain_names == {"read_file", "search", "glob", "list_files", "related_files", "review_diff"}
    assert tool_schemas_for_mode("commit_message") == []
    for schema in TOOL_SCHEMAS:
        assert schema["description"]
        assert schema["input_schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_read_file_returns_numbered_lines(tmp_path):
    (tmp_path / "a.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    result = await run_tool("read_file", {"path": "a.py", "offset": 2, "limit": 1}, make_context(tmp_path))
    assert result.success
    assert isinstance(result.duration_ms, int)
    assert "2\tline2" in result.text
    assert "line1" not in result.text
    assert "has 3 lines" in result.text


@pytest.mark.asyncio
async def test_read_file_missing(tmp_path):
    result = await run_tool("read_file", {"path": "nope.py"}, make_context(tmp_path))
    assert not result.success
    assert "does not exist" in result.error


@pytest.mark.asyncio
async def test_run_tool_validates_required_arguments(tmp_path):
    result = await run_tool("read_file", {}, make_context(tmp_path))
    assert not result.success
    assert "argument validation failed" in result.error
    assert result.data["validation_error"] == "missing required field: path"
    assert isinstance(result.duration_ms, int)


@pytest.mark.asyncio
async def test_run_tool_validates_argument_types(tmp_path):
    (tmp_path / "a.py").write_text("line1\n", encoding="utf-8")
    result = await run_tool("read_file", {"path": "a.py", "offset": "2"}, make_context(tmp_path))
    assert not result.success
    assert result.data["validation_error"] == "offset must be an integer"


@pytest.mark.asyncio
async def test_read_file_protected(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    result = await run_tool("read_file", {"path": ".env"}, make_context(tmp_path))
    assert not result.success


@pytest.mark.asyncio
async def test_read_file_rejects_mandatory_git_config(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\nurl = https://token@example.test/repo.git\n',
        encoding="utf-8",
    )

    result = await run_tool("read_file", {"path": ".git/config"}, make_context(tmp_path))

    assert not result.success
    assert "protected path" in result.error


@pytest.mark.asyncio
async def test_read_file_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)

    result = await run_tool("read_file", {"path": "link.txt"}, make_context(tmp_path))

    assert not result.success
    assert "workspace" in result.error


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
async def test_search_python_fallback_rejects_symlink_escape(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside-search.txt"
    outside.write_text("provider needle secret\n", encoding="utf-8")
    (tmp_path / "linked.py").symlink_to(outside)
    monkeypatch.setattr("app.tools.registry.shutil.which", lambda _name: None)

    result = await run_tool("search", {"query": "needle"}, make_context(tmp_path))

    assert result.success
    assert "linked.py" not in result.text
    assert "provider needle secret" not in result.text


@pytest.mark.asyncio
async def test_list_files_hides_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside-list"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "linked-dir").symlink_to(outside, target_is_directory=True)

    result = await run_tool("list_files", {"max_depth": 2}, make_context(tmp_path))

    assert result.success
    assert "linked-dir" not in result.text
    assert "secret.txt" not in result.text


@pytest.mark.asyncio
async def test_tool_output_redacts_known_runtime_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret-value")
    (tmp_path / "ordinary.txt").write_text("value=provider-secret-value\n", encoding="utf-8")

    result = await run_tool("read_file", {"path": "ordinary.txt"}, make_context(tmp_path))

    assert result.success
    assert "provider-secret-value" not in result.text
    assert "[REDACTED]" in result.text


@pytest.mark.asyncio
async def test_read_file_offset_out_of_range(tmp_path):
    (tmp_path / "a.py").write_text("line1\nline2\n", encoding="utf-8")
    result = await run_tool("read_file", {"path": "a.py", "offset": 99}, make_context(tmp_path))
    assert not result.success
    assert "exceeds" in result.error


@pytest.mark.asyncio
async def test_search_no_match(tmp_path):
    (tmp_path / "a.py").write_text("nothing here\n", encoding="utf-8")
    result = await run_tool("search", {"query": "zzz_not_found"}, make_context(tmp_path))
    assert result.success
    assert "no matches" in result.text


@pytest.mark.asyncio
async def test_search_reports_rg_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("app.tools.registry.shutil.which", lambda name: "/fake/rg")

    async def fake_run_command(command, *, cwd, timeout, **_kwargs):
        return CommandResult(command=list(command), returncode=-9, stderr="command timed out: 30s", timed_out=True)

    monkeypatch.setattr("app.tools.registry.run_command", fake_run_command)
    result = await run_tool("search", {"query": "needle"}, make_context(tmp_path))

    assert not result.success
    assert "search timed out" in result.error
    assert isinstance(result.duration_ms, int)


@pytest.mark.asyncio
async def test_unknown_tool(tmp_path):
    result = await run_tool("mystery", {}, make_context(tmp_path))
    assert not result.success


@pytest.mark.asyncio
async def test_related_files_finds_source_test_pair_and_references(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def login():\n    return True\n", encoding="utf-8")
    (tmp_path / "tests" / "test_auth.py").write_text("from src.auth import login\n", encoding="utf-8")
    (tmp_path / "src" / "routes.py").write_text("from src.auth import login\n", encoding="utf-8")

    result = await run_tool("related_files", {"path": "src/auth.py"}, make_context(tmp_path))

    assert result.success
    paths = [item["path"] for item in result.data["related"]]
    assert "tests/test_auth.py" in paths
    assert "src/routes.py" in paths
    test_item = next(item for item in result.data["related"] if item["path"] == "tests/test_auth.py")
    assert "source_test_pair" in test_item["reasons"]
    route_item = next(item for item in result.data["related"] if item["path"] == "src/routes.py")
    assert "reference" in route_item["reasons"]
    assert route_item["line"] == 1


@pytest.mark.asyncio
async def test_related_files_from_test_finds_source(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def login():\n    return True\n", encoding="utf-8")
    (tmp_path / "tests" / "test_auth.py").write_text("from src.auth import login\n", encoding="utf-8")

    result = await run_tool("related_files", {"path": "tests/test_auth.py"}, make_context(tmp_path))

    assert result.success
    paths = [item["path"] for item in result.data["related"]]
    assert "src/auth.py" in paths


@pytest.mark.asyncio
async def test_related_files_skips_protected_matches(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "secret").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def login():\n    return True\n", encoding="utf-8")
    (tmp_path / "secret" / "test_auth.py").write_text("from src.auth import login\n", encoding="utf-8")

    context = ToolContext(workspace=tmp_path, protected_paths=["secret/**"])
    result = await run_tool("related_files", {"path": "src/auth.py"}, context)

    assert result.success
    paths = [item["path"] for item in result.data["related"]]
    assert "secret/test_auth.py" not in paths


@pytest.mark.asyncio
async def test_related_files_skips_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside-related.py"
    outside.write_text("from src.auth import login\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def login():\n    return True\n", encoding="utf-8")
    (tmp_path / "linked_auth.py").symlink_to(outside)

    result = await run_tool("related_files", {"path": "src/auth.py"}, make_context(tmp_path))

    assert result.success
    assert "linked_auth.py" not in result.text


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
async def test_related_files_supports_configured_read_only_workspace(tmp_path):
    main = tmp_path / "main"
    api = tmp_path / "api"
    main.mkdir()
    (api / "src").mkdir(parents=True)
    (api / "tests").mkdir()
    (api / "src" / "service.py").write_text("def helper():\n    return 'ok'\n", encoding="utf-8")
    (api / "tests" / "test_service.py").write_text("from src.service import helper\n", encoding="utf-8")

    context = ToolContext(
        workspace=main,
        workspace_refs=[WorkspaceRef(name="api", path=str(api), mode="read_only")],
    )
    result = await run_tool("related_files", {"path": "src/service.py", "workspace": "api"}, context)

    assert result.success
    assert "api:tests/test_service.py" in result.text
    assert result.data["workspace"] == "api"


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
    assert "workspace boundary" in result.error


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
    assert "unknown workspace" in result.error


class _CountingTool:
    """A tool defined entirely outside the built-in set."""

    def __init__(self, spec: ToolSpec) -> None:
        self.spec = spec
        self.calls = 0

    async def run(self, args, context):
        self.calls += 1
        return ToolResult(success=True, text=f"counted {args.get('label', '')}".strip())


def _spec(name: str, *, read_only: bool, approval: str = "gate", hidden: frozenset[str] = frozenset()) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} test tool",
        read_only=read_only,
        approval=approval,
        hidden_in_modes=hidden,
        input_schema={"type": "object", "properties": {"label": {"type": "string"}}},
    )


@pytest.mark.asyncio
async def test_a_custom_read_only_tool_is_visible_and_allowed_without_touching_core(tmp_path):
    """The point of the registry: one registration, no edits elsewhere.

    Adding a tool previously meant editing the schema list, the dispatch chain,
    the registry's read-only names and the policy engine's separate copy of them.
    """
    tool = _CountingTool(_spec("inspect_manifest", read_only=True, approval="none"))
    registry = build_default_registry()
    registry.register(tool)

    # Visible to the model, including in the read-only modes.
    assert "inspect_manifest" in {schema["name"] for schema in registry.schemas_for_mode("default")}
    assert "inspect_manifest" in {schema["name"] for schema in registry.schemas_for_mode("review")}

    # The policy engine allows it purely because the spec says it is read-only.
    spec = registry.spec_for("inspect_manifest")
    assert spec is not None
    decision = PolicyEngine().gate("inspect_manifest", {}, mode="review", spec=spec)
    assert decision.verdict == "allow"

    # And it is dispatched by lookup, not by a name branch.
    result = await registry.run("inspect_manifest", {"label": "x"}, ToolContext(workspace=tmp_path))
    assert result.success is True
    assert result.text == "counted x"
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_a_custom_write_tool_is_hidden_and_denied_in_read_only_modes(tmp_path):
    tool = _CountingTool(_spec("mutate_manifest", read_only=False, hidden=WRITE_HIDDEN_MODES))
    registry = build_default_registry()
    registry.register(tool)

    assert "mutate_manifest" not in {schema["name"] for schema in registry.schemas_for_mode("review")}
    spec = registry.spec_for("mutate_manifest")
    assert PolicyEngine().gate("mutate_manifest", {}, mode="review", spec=spec).verdict == "deny"
    # Never executed: the schema hides it and the policy layer refuses it.
    assert tool.calls == 0


def test_registry_rejects_a_duplicate_name(tmp_path):
    """A silently replaced tool would be a confusing way to lose behaviour."""
    registry = build_default_registry()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_CountingTool(_spec("bash", read_only=False)))


def test_specs_are_the_single_source_of_read_only_truth():
    """Regression guard for the drift this refactor removed.

    The policy engine used to keep its own set of read-only tool names next to the
    registry's; nothing stopped the two from disagreeing.
    """
    import app.agent.policy as policy_module

    assert not hasattr(policy_module, "READ_ONLY_TOOLS_V2")
    read_only = {spec.name for spec in DEFAULT_REGISTRY.specs() if spec.read_only}
    assert read_only == {
        "read_file", "search", "glob", "list_files", "related_files", "review_diff", "read_output",
    }


def test_edit_file_declares_diff_approval():
    """The Agent Loop routes on this, rather than on the tool's name."""
    spec = DEFAULT_REGISTRY.spec_for("edit_file")
    assert spec is not None
    assert spec.approval == "diff"
    assert [s.name for s in DEFAULT_REGISTRY.specs() if s.approval == "diff"] == ["edit_file"]
