from __future__ import annotations

import json
from typing import Any

from app.agent.types import AgentRequest
from app.agent.utils import localized


def model_purpose_for_mode(mode: str) -> str:
    if mode == "review":
        return "reviewer"
    return "summarizer"


def build_model_messages(request: AgentRequest, observations: list[dict[str, Any]]) -> list[dict[str, str]]:
    language_name = "English" if request.language.startswith("en") else "中文"
    if request.mode == "review":
        system = (
            f"你是 aicode 的只读代码审查助手。使用{language_name}回答。"
            "只基于工具输出做结论，不要编造没有证据的问题。"
            "不得建议已经修改代码；review 模式只允许只读分析。"
            "优先输出: 结论、必须处理的问题、可选改进、建议验证命令。"
        )
    else:
        system = (
            f"你是 aicode 的 coding agent 摘要助手。使用{language_name}回答。"
            "基于工具输出给出简洁进展总结和下一步建议，不要编造。"
        )

    payload = {
        "user_request": request.message,
        "mode": request.mode,
        "workspace": request.workspace,
        "tool_observations": observations,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def final_summary_text(request: AgentRequest, observations: list[dict[str, Any]], model_text: str, provider: str) -> str:
    cleaned = model_text.strip()
    if provider != "stub" and cleaned:
        return cleaned

    if request.mode == "review":
        prefix = localized(
            request.language,
            "模型 provider 未配置，以下为确定性 Review 结果：",
            "Model provider is not configured. Deterministic review result:",
        )
        return prefix + "\n\n" + format_review_fallback_summary(request.language, observations)

    if provider == "stub" or not cleaned:
        patch = find_last_observation(observations, "apply_patch")
        if patch is not None:
            return format_patch_fallback_summary(request.language, patch)
        return localized(
            request.language,
            "Agent 工具循环已连通。Runtime 已通过结构化步骤读取工作区、检查 git 状态，并支持写入前 inline diff 确认。",
            "Agent tool loop is connected. The runtime used structured steps to inspect the workspace, check git status, and supports inline diff approval before writes.",
        )
    return cleaned


def format_patch_fallback_summary(language: str, patch: dict[str, Any]) -> str:
    status = str(patch.get("status") or "unknown")
    operation = str(patch.get("operation") or "patch")
    files = patch.get("files") if isinstance(patch.get("files"), list) else []
    file_text = ", ".join(str(item) for item in files) if files else "unknown"
    verification = patch.get("verification") if isinstance(patch.get("verification"), dict) else {}
    verification_status = str(verification.get("status") or "")
    command = str(verification.get("command") or "")
    reason = str(patch.get("reason") or verification.get("reason") or "")
    analysis = verification.get("analysis") if isinstance(verification.get("analysis"), dict) else {}

    if language.startswith("en"):
        lines = ["Patch Result"]
        if status == "applied":
            lines.append(f"- Applied `{operation}` to: {file_text}.")
            lines.append(format_verification_line_en(verification_status, command, reason, analysis))
        elif status == "rejected":
            lines.append(f"- Patch was rejected by the user: {file_text}.")
        elif status == "timeout":
            lines.append(f"- Patch approval timed out: {file_text}.")
        elif status == "denied":
            lines.append(f"- Patch was denied by policy: {reason}.")
        else:
            lines.append(f"- Patch did not apply: {reason or status}.")
        return "\n".join(lines)

    lines = ["Patch 结果"]
    if status == "applied":
        lines.append(f"- 已执行 `{operation}`，文件: {file_text}。")
        lines.append(format_verification_line_zh(verification_status, command, reason, analysis))
    elif status == "rejected":
        lines.append(f"- 用户拒绝应用 patch，文件: {file_text}。")
    elif status == "timeout":
        lines.append(f"- patch 等待确认超时，文件: {file_text}。")
    elif status == "denied":
        lines.append(f"- 策略拒绝写入: {reason}。")
    else:
        lines.append(f"- patch 未应用: {reason or status}。")
    return "\n".join(lines)


def format_verification_line_zh(status: str, command: str, reason: str, analysis: dict[str, Any] | None = None) -> str:
    if status == "passed":
        return f"- 验证通过: `{command}`。"
    if status == "failed":
        summary = verification_analysis_summary(analysis)
        suffix = f" 失败摘要: {summary}" if summary else ""
        return f"- 验证失败: `{command}`。{suffix}"
    if status == "skipped":
        return f"- 验证跳过: {reason or '未发现可自动运行的测试命令'}。"
    if status == "denied":
        return f"- 验证未运行: {reason or '验证命令未通过安全策略'}。"
    return "- 验证未运行。"


def format_verification_line_en(status: str, command: str, reason: str, analysis: dict[str, Any] | None = None) -> str:
    if status == "passed":
        return f"- Verification passed: `{command}`."
    if status == "failed":
        summary = verification_analysis_summary(analysis)
        suffix = f" Summary: {summary}" if summary else ""
        return f"- Verification failed: `{command}`.{suffix}"
    if status == "skipped":
        return f"- Verification skipped: {reason or 'no test command detected'}."
    if status == "denied":
        return f"- Verification did not run: {reason or 'verification command was denied by policy'}."
    return "- Verification did not run."


def verification_analysis_summary(analysis: dict[str, Any] | None) -> str:
    if not analysis:
        return ""
    summary = str(analysis.get("summary") or "").strip()
    if summary:
        return summary
    failures = analysis.get("failures") if isinstance(analysis.get("failures"), list) else []
    if not failures:
        return ""
    first = failures[0] if isinstance(failures[0], dict) else {}
    name = str(first.get("name") or first.get("path") or "").strip()
    message = str(first.get("message") or "").strip()
    if name and message:
        return f"{name}: {message}"
    return name or message


def review_observation_text(observations: list[dict[str, Any]]) -> str:
    for observation in observations:
        if observation.get("tool") == "review_diff" and observation.get("success"):
            text = str(observation.get("text") or "").strip()
            if text:
                return text
    return "Review 未产生可用结果。"


def format_review_fallback_summary(language: str, observations: list[dict[str, Any]]) -> str:
    review = find_observation(observations, "review_diff")
    if not review or not review.get("success"):
        return localized(language, "结论\nReview 未产生可用结果。", "Conclusion\nReview did not produce a usable result.")

    data = review.get("data") if isinstance(review.get("data"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    findings = data.get("findings") if isinstance(data.get("findings"), list) else []
    finding_count = int_value(summary.get("finding_count"), len(findings))
    severity = summary.get("by_severity") if isinstance(summary.get("by_severity"), dict) else {}
    high = int_value(severity.get("high"), 0)
    medium = int_value(severity.get("medium"), 0)
    low = int_value(severity.get("low"), 0)

    if language.startswith("en"):
        return format_review_fallback_summary_en(observations, findings, finding_count, high, medium, low)
    return format_review_fallback_summary_zh(observations, findings, finding_count, high, medium, low)


def format_review_fallback_summary_zh(
    observations: list[dict[str, Any]], findings: list[Any], finding_count: int, high: int, medium: int, low: int
) -> str:
    lines: list[str] = ["结论"]
    if finding_count == 0:
        lines.append("未发现确定性风险。")
    else:
        lines.append(f"发现 {finding_count} 个确定性问题：high={high} medium={medium} low={low}。")

    lines.extend(["", "风险"])
    if finding_count == 0:
        lines.append("- 无必须处理问题。")
    else:
        for finding in normalized_findings(findings)[:8]:
            lines.append(f"- [{finding['severity']}] {finding['location']} {finding['title']}：{finding['message']}")
        if finding_count > 8:
            lines.append(f"- 其余 {finding_count - 8} 个问题已省略，请查看上方 review_diff 输出。")

    lines.extend(["", "建议验证"])
    test_command = detected_test_command(observations)
    if test_command:
        lines.append(f"- 运行 `{test_command}`。")
    else:
        lines.append("- 运行项目测试。")
    lines.append("- 修复后重新运行 `aicode review`。")
    return "\n".join(lines)


def format_review_fallback_summary_en(
    observations: list[dict[str, Any]], findings: list[Any], finding_count: int, high: int, medium: int, low: int
) -> str:
    lines: list[str] = ["Conclusion"]
    if finding_count == 0:
        lines.append("No deterministic risks found.")
    else:
        lines.append(f"Found {finding_count} deterministic issues: high={high} medium={medium} low={low}.")

    lines.extend(["", "Risks"])
    if finding_count == 0:
        lines.append("- No must-fix issues.")
    else:
        for finding in normalized_findings(findings)[:8]:
            lines.append(f"- [{finding['severity']}] {finding['location']} {finding['title']}: {finding['message']}")
        if finding_count > 8:
            lines.append(f"- {finding_count - 8} more findings omitted; see the review_diff output above.")

    lines.extend(["", "Suggested Verification"])
    test_command = detected_test_command(observations)
    if test_command:
        lines.append(f"- Run `{test_command}`.")
    else:
        lines.append("- Run the project test suite.")
    lines.append("- Run `aicode review` again after fixes.")
    return "\n".join(lines)


def normalized_findings(findings: list[Any]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for raw in findings:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or ".")
        line = raw.get("line")
        location = path + (f":{line}" if line is not None else "")
        normalized.append(
            {
                "severity": str(raw.get("severity") or "low"),
                "location": location,
                "title": str(raw.get("title") or "问题"),
                "message": str(raw.get("message") or ""),
            }
        )
    return normalized


def detected_test_command(observations: list[dict[str, Any]]) -> str | None:
    project = find_observation(observations, "detect_project")
    if not project or not isinstance(project.get("data"), dict):
        return None
    command = project["data"].get("test_command")
    if not command:
        return None
    return str(command)


def find_observation(observations: list[dict[str, Any]], tool: str) -> dict[str, Any] | None:
    for observation in observations:
        if observation.get("tool") == tool:
            return observation
    return None


def find_last_observation(observations: list[dict[str, Any]], tool: str) -> dict[str, Any] | None:
    for observation in reversed(observations):
        if observation.get("tool") == tool:
            return observation
    return None


def int_value(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
