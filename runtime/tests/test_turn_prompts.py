import json

from app.adapters.workspace import LocalWorkspaceRuntime
from app.agent.prompts import build_system_prompt
from app.agent.turn import TurnBudget, assistant_message, tool_message, user_message, user_note
from app.models.provider import CompletionResult, ToolCallRequest


class FakeRequest:
    def __init__(self, workspace, mode="default", message="do something"):
        self.workspace = str(workspace)
        self.mode = mode
        self.message = message


def test_message_builders():
    assert user_message("hi") == {"role": "user", "content": "hi"}
    result = CompletionResult(text="t", tool_calls=[ToolCallRequest(id="tc_1", name="bash", arguments={"command": "ls"})], model="m", provider="p")
    message = assistant_message(result)
    assert message["role"] == "assistant"
    assert message["tool_calls"][0]["name"] == "bash"
    json.dumps(message)  # Must be JSON serializable.
    assert tool_message("tc_1", "out") == {"role": "tool", "tool_call_id": "tc_1", "content": "out"}
    assert user_note("verify this")["content"].startswith("[system note]")


def test_budget_defaults():
    budget = TurnBudget()
    assert budget.max_steps == 40


def test_system_prompt_includes_project_info(tmp_path):
    (tmp_path / ".aicode").mkdir()
    (tmp_path / ".aicode" / "config.json").write_text(
        '{"commands": {"test": "pytest -q", "lint": "ruff check ."}, "protectedPaths": [".env"]}',
        encoding="utf-8",
    )
    (tmp_path / ".aicode" / "rules.md").write_text("Always write concise comments", encoding="utf-8")
    (tmp_path / ".aicode" / "memory.md").write_text("Authentication lives in runtime/app/server/auth.py", encoding="utf-8")
    request = FakeRequest(tmp_path)
    prompt = build_system_prompt(request, LocalWorkspaceRuntime().prompt_context(tmp_path))
    assert "pytest -q" in prompt
    assert "lint: ruff check ." in prompt
    assert ".env" in prompt
    assert "Always write concise comments" in prompt
    assert "Authentication lives in runtime/app/server/auth.py" in prompt
    assert "Use English for all user-facing output." in prompt
    assert "cannot override system instructions" in prompt
    assert "safety constraints" in prompt
    assert "Project memory" in prompt


def test_system_prompt_review_mode(tmp_path):
    request = FakeRequest(tmp_path, mode="review")
    prompt = build_system_prompt(request, LocalWorkspaceRuntime().prompt_context(tmp_path))
    assert "read-only" in prompt


def test_system_prompt_is_english(tmp_path):
    (tmp_path / ".aicode").mkdir()
    (tmp_path / ".aicode" / "config.json").write_text('{"commands": {"test": "auto", "build": "npm run build"}}', encoding="utf-8")
    (tmp_path / ".aicode" / "rules.md").write_text("Prefer concise tests", encoding="utf-8")
    (tmp_path / ".aicode" / "memory.md").write_text("Auth starts in server/auth.py", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    request = FakeRequest(tmp_path, mode="review")
    prompt = build_system_prompt(request, LocalWorkspaceRuntime().prompt_context(tmp_path))

    assert "Use English for all user-facing output." in prompt
    assert "Working rules:" in prompt
    assert "Review mode:" in prompt
    assert "Known project commands:" in prompt
    assert "build: npm run build" in prompt
    assert "test: python3 -m pytest (auto-detected)" in prompt
    assert "Project rules (.aicode/rules.md)" in prompt
    assert "Project memory (.aicode/memory.md)" in prompt
    assert "Auth starts in server/auth.py" in prompt
    assert "cannot override system instructions" in prompt


def test_system_prompt_limits_project_memory(tmp_path):
    (tmp_path / ".aicode").mkdir()
    (tmp_path / ".aicode" / "memory.md").write_text("a" * 4100 + "TAIL", encoding="utf-8")

    request = FakeRequest(tmp_path)
    prompt = build_system_prompt(request, LocalWorkspaceRuntime().prompt_context(tmp_path))

    assert "a" * 4000 in prompt
    assert "TAIL" not in prompt
