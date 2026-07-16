from pathlib import Path

from app.project.detect import detect_test_command
from app.server.main import MessageRequest, build_model_messages, detect_append_request, final_summary_text, model_purpose_for_mode


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
        [{"tool": "review_diff", "success": True, "text": "Review 结果: 未发现确定性风险。"}],
        "Runtime 骨架已连接。",
        "stub",
    )

    assert "模型 provider 未配置" in summary
    assert "Review 结果: 未发现确定性风险。" in summary
