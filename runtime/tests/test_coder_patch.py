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
