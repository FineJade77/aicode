import json

from app.agent.prompts import build_system_prompt
from app.agent.turn import TurnBudget, assistant_message, tool_message, user_message, user_note
from app.models.provider import CompletionResult, ToolCallRequest


class FakeRequest:
    def __init__(self, workspace, mode="default", language="zh-CN", message="做点事"):
        self.workspace = str(workspace)
        self.mode = mode
        self.language = language
        self.message = message


def test_message_builders():
    assert user_message("hi") == {"role": "user", "content": "hi"}
    result = CompletionResult(text="t", tool_calls=[ToolCallRequest(id="tc_1", name="bash", arguments={"command": "ls"})], model="m", provider="p")
    message = assistant_message(result)
    assert message["role"] == "assistant"
    assert message["tool_calls"][0]["name"] == "bash"
    json.dumps(message)  # 必须可序列化
    assert tool_message("tc_1", "out") == {"role": "tool", "tool_call_id": "tc_1", "content": "out"}
    assert user_note("请验证")["content"].startswith("[系统提示]")


def test_budget_defaults():
    budget = TurnBudget()
    assert budget.max_steps == 40


def test_system_prompt_includes_project_info(tmp_path):
    (tmp_path / ".aicode").mkdir()
    (tmp_path / ".aicode" / "config.json").write_text('{"commands": {"test": "pytest -q"}, "protectedPaths": [".env"]}', encoding="utf-8")
    (tmp_path / ".aicode" / "rules.md").write_text("永远写中文注释", encoding="utf-8")
    prompt = build_system_prompt(FakeRequest(tmp_path))
    assert "pytest -q" in prompt
    assert ".env" in prompt
    assert "永远写中文注释" in prompt
    assert "中文" in prompt  # 语言指令


def test_system_prompt_review_mode(tmp_path):
    prompt = build_system_prompt(FakeRequest(tmp_path, mode="review"))
    assert "只读" in prompt
