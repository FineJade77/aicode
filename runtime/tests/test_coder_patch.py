import json
from types import SimpleNamespace

from app.agent.patch_flow import build_coder_patch_messages, parse_coder_patch


def test_parse_coder_patch_accepts_replace_json() -> None:
    patch = parse_coder_patch(
        """```json
{"action":"patch","operation":"replace","path":"README.md","old_text":"old","new_text":"new","reason":"fix text"}
```"""
    )

    assert patch is not None
    assert patch.operation == "replace"
    assert patch.path == "README.md"
    assert patch.old_text == "old"
    assert patch.new_text == "new"


def test_parse_coder_patch_accepts_operations_json() -> None:
    patch = parse_coder_patch(
        """{
  "action": "patch",
  "schema_version": 1,
  "operations": [
    {"operation": "replace", "path": "README.md", "old_text": "old", "new_text": "new"},
    {"operation": "create", "path": "TODO.md", "content": "todo"}
  ],
  "reason": "update docs"
}"""
    )

    assert patch is not None
    assert len(patch.operations) == 2
    assert patch.operations[0].operation == "replace"
    assert patch.operations[0].path == "README.md"
    assert patch.operations[1].operation == "create"
    assert patch.operations[1].path == "TODO.md"


def test_parse_coder_patch_accepts_same_file_operation_sequence() -> None:
    patch = parse_coder_patch(
        """{
  "action": "patch",
  "operations": [
    {"operation": "replace", "path": "README.md", "old_text": "old", "new_text": "new"},
    {"operation": "append", "path": "./README.md", "text": "more"}
  ]
}"""
    )

    assert patch is not None
    assert len(patch.operations) == 2
    assert patch.operations[0].path == "README.md"
    assert patch.operations[1].path == "./README.md"


def test_parse_coder_patch_accepts_delete_and_rename() -> None:
    patch = parse_coder_patch(
        """{
  "action": "patch",
  "operations": [
    {"operation": "delete", "path": "old.txt"},
    {"operation": "rename", "path": "src/old.py", "new_path": "src/new.py"}
  ]
}"""
    )

    assert patch is not None
    assert patch.operations[0].operation == "delete"
    assert patch.operations[1].operation == "rename"
    assert patch.operations[1].new_path == "src/new.py"


def test_parse_coder_patch_rejects_unsupported_schema_version() -> None:
    patch = parse_coder_patch(
        '{"action":"patch","schema_version":99,"operation":"replace","path":"README.md","old_text":"old","new_text":"new"}'
    )

    assert patch is None


def test_parse_coder_patch_rejects_cross_workspace_path() -> None:
    patch = parse_coder_patch(
        '{"action":"patch","operation":"replace","path":"api:src/service.py","old_text":"old","new_text":"new"}'
    )

    assert patch is None


def test_parse_coder_patch_allows_empty_replacement() -> None:
    patch = parse_coder_patch(
        '{"action":"patch","operation":"replace","path":"README.md","old_text":"remove me","new_text":""}'
    )

    assert patch is not None
    assert patch.new_text == ""


def test_parse_coder_patch_rejects_path_escape() -> None:
    patch = parse_coder_patch(
        '{"action":"patch","operation":"create","path":"../secret.txt","content":"secret"}'
    )

    assert patch is None


def test_build_coder_patch_messages_includes_observations() -> None:
    request = SimpleNamespace(message="修复 README", mode="default", workspace="/repo", language="zh-CN")
    messages = build_coder_patch_messages(request, [{"tool": "read_file", "text": "# README\\nold"}])

    assert messages[0]["role"] == "system"
    assert "只能输出一个 JSON object" in messages[0]["content"]
    assert "read_file" in messages[1]["content"]
    assert "修复 README" in messages[1]["content"]


def test_build_coder_patch_messages_uses_context_budget() -> None:
    request = SimpleNamespace(message="修复大文件", mode="default", workspace="/repo", language="zh-CN")
    messages = build_coder_patch_messages(
        request,
        [{"tool": "read_file", "text": "# big.py\n" + "x" * 30_000 + "\nTAIL", "data": {"path": "big.py"}}],
    )
    payload = json.loads(messages[1]["content"])

    observation = payload["observations"][0]
    assert payload["context_budget"]["compacted"] is True
    assert observation["text"].startswith("# big.py")
    assert observation["text"].endswith("TAIL")
    assert "[CONTEXT COMPACTED:" in observation["text"]
