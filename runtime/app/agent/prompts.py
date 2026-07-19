from __future__ import annotations

from pathlib import Path
from typing import Any

from app.project.config import load_project_config
from app.project.detect import detect_test_command

VERIFY_NOTE_ZH = "编辑已应用。请运行相关测试或命令验证改动；如果验证失败请继续修复，连续 3 次修复失败请停止并汇报现状。"
VERIFY_NOTE_EN = "Edits applied. Run relevant tests to verify; keep fixing on failure, stop and report after 3 consecutive failed attempts."
BUDGET_NOTE_ZH = "已达到本轮步数上限。请立即停止调用工具，总结目前完成了什么、剩余什么。"
BUDGET_NOTE_EN = "Step limit reached. Stop calling tools now and summarize what is done and what remains."

MODE_INSTRUCTIONS_ZH = {
    "review": "当前是 review 模式：你只有只读工具，禁止任何修改建议之外的写入动作。审查当前 git diff（可用 review_diff 获取规则结果），输出结构化审查结论。",
    "diff": "查看当前 git diff（bash: git diff）并总结变更要点。",
    "test": "发现并运行本项目的测试命令，报告结果；如有失败，定位原因。",
    "explain": "解释用户指定的文件或符号，先定位再阅读，不要修改任何文件。",
}


def build_system_prompt(request: Any) -> str:
    language_line = (
        "所有面向用户的输出使用英文。" if str(request.language).startswith("en") else "所有面向用户的输出使用中文。"
    )
    workspace = Path(request.workspace)
    config = load_project_config(workspace)
    test_command = ""
    config_test = config.commands.get("test")
    if config_test and config_test != "auto":
        test_command = config_test
    else:
        detected = detect_test_command(workspace)
        test_command = detected or ""
    rules_path = workspace / ".aicode" / "rules.md"
    rules_text = rules_path.read_text("utf-8", errors="replace")[:4000] if rules_path.is_file() else ""
    workspaces = ", ".join(f"{ref.name}（只读）" for ref in config.workspaces) or "无"

    sections = [
        "你是 aicode，一个在用户本机工作区工作的 coding agent。通过提供的工具探索代码、执行命令、修改文件。",
        language_line,
        "工作准则：",
        "- 修改文件前必须先用 read_file 读到要改的原文；edit_file 的 old_text 必须与文件原文完全一致且唯一。",
        "- 每次 edit_file 都会展示 diff 等用户确认；被拒绝时调整方案或询问，不要原样重试。",
        "- 命令被策略拒绝时换安全的替代做法，或把需要用户手动执行的命令写进最终答复。",
        "- 完成修改后要运行测试验证；工具输出被截断时可用 offset 继续读。",
        "- 任务完成或无事可做时直接输出结论文本，不要空转调用工具。",
        f"工作区: {request.workspace}",
        f"测试命令: {test_command or '未检测到，可自行探测'}",
        f"受保护路径（禁止读写）: {', '.join(config.protected_paths)}",
        f"额外只读 workspace: {workspaces}",
    ]
    mode_line = MODE_INSTRUCTIONS_ZH.get(request.mode)
    if mode_line:
        sections.append(f"本次任务模式: {mode_line}")
    if rules_text:
        sections.append(f"项目规则（.aicode/rules.md）:\n{rules_text}")
    return "\n".join(sections)
