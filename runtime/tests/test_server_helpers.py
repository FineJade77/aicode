from pathlib import Path

import pytest

from app.project.detect import detect_test_command
from app.server.main import MessageRequest, build_model_messages, detect_append_request, final_summary_text, model_purpose_for_mode, review_rules


def test_detect_test_command_for_go_work(tmp_path: Path) -> None:
    (tmp_path / "go.work").write_text("go 1.22\n\nuse ./cli\n", encoding="utf-8")

    assert detect_test_command(tmp_path) == "go test ./cli/..."


def test_detect_append_request() -> None:
    assert detect_append_request("append README.md hello world") == ("README.md", "hello world")
    assert detect_append_request("追加 README.md 你好") == ("README.md", "你好")


def test_review_mode_uses_reviewer_model_purpose() -> None:
    assert model_purpose_for_mode("review") == "reviewer"
    assert model_purpose_for_mode("default") == "summarizer"


def test_review_model_prompt_includes_tool_observations() -> None:
    request = MessageRequest(message="请审查当前代码变更。", mode="review", workspace="/repo", language="zh-CN")
    messages = build_model_messages(
        request,
        [
            {
                "tool": "review_diff",
                "success": True,
                "risk_level": "low",
                "requires_approval": False,
                "text": "Review 结果: 未发现确定性风险。",
                "data": {"summary": {"finding_count": 0}},
            }
        ],
    )

    assert messages[0]["role"] == "system"
    assert "只读代码审查助手" in messages[0]["content"]
    assert "review_diff" in messages[1]["content"]
    assert "finding_count" in messages[1]["content"]


def test_review_final_falls_back_to_deterministic_text_for_stub() -> None:
    request = MessageRequest(message="请审查当前代码变更。", mode="review", workspace="/repo", language="zh-CN")
    summary = final_summary_text(
        request,
        [
            {"tool": "detect_project", "success": True, "data": {"test_command": "go test ./..."}},
            {
                "tool": "review_diff",
                "success": True,
                "text": "Review 结果: 未发现确定性风险。",
                "data": {"summary": {"finding_count": 0, "by_severity": {"high": 0, "medium": 0, "low": 0}}, "findings": []},
            },
        ],
        "Runtime 骨架已连接。",
        "stub",
    )

    assert "模型 provider 未配置" in summary
    assert "结论\n未发现确定性风险。" in summary
    assert "风险\n- 无必须处理问题。" in summary
    assert "建议验证\n- 运行 `go test ./...`。" in summary


def test_review_final_groups_findings_for_stub() -> None:
    request = MessageRequest(message="请审查当前代码变更。", mode="review", workspace="/repo", language="zh-CN")
    summary = final_summary_text(
        request,
        [
            {"tool": "detect_project", "success": True, "data": {"test_command": "python3 -m pytest"}},
            {
                "tool": "review_diff",
                "success": True,
                "data": {
                    "summary": {"finding_count": 1, "by_severity": {"high": 1, "medium": 0, "low": 0}},
                    "findings": [
                        {
                            "severity": "high",
                            "path": ".env",
                            "line": 2,
                            "title": "新增行包含疑似密钥",
                            "message": "新增内容匹配凭证或私钥特征。",
                        }
                    ],
                },
            },
        ],
        "Runtime 骨架已连接。",
        "stub",
    )

    assert "发现 1 个确定性问题：high=1 medium=0 low=0。" in summary
    assert "- [high] .env:2 新增行包含疑似密钥：新增内容匹配凭证或私钥特征。" in summary
    assert "- 运行 `python3 -m pytest`。" in summary


@pytest.mark.asyncio
async def test_review_rules_endpoint_uses_workspace_config(tmp_path: Path) -> None:
    config_dir = tmp_path / ".aicode"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        '{"review":{"disabledRules":["large_diff"],"largeDiffThreshold":1200,"maxFindings":25}}',
        encoding="utf-8",
    )

    data = await review_rules(str(tmp_path))
    rules = {rule["id"]: rule for rule in data["rules"]}

    assert data["effective_config"]["large_diff_threshold"] == 1200
    assert data["effective_config"]["max_findings"] == 25
    assert rules["large_diff"]["enabled"] is False
