from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentStep:
    action: str
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    source: str = "rules"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action,
            "reason": self.reason,
            "source": self.source,
        }
        if self.tool:
            payload["tool"] = self.tool
            payload["args"] = self.args
            payload["step_key"] = step_key(self.tool, self.args)
        return payload


TOOL_DOCS: list[dict[str, Any]] = [
    {
        "name": "list_files",
        "read_only": True,
        "description": "List workspace files.",
        "args": {"path": ".", "max_depth": 1, "limit": 40},
    },
    {
        "name": "detect_project",
        "read_only": True,
        "description": "Detect languages, package manager, and test command.",
        "args": {},
    },
    {
        "name": "git_status",
        "read_only": True,
        "description": "Read git status --short.",
        "args": {},
    },
    {
        "name": "git_diff",
        "read_only": True,
        "description": "Read current git diff.",
        "args": {"path": "optional relative path"},
    },
    {
        "name": "git_show",
        "read_only": True,
        "description": "Read git show --stat --oneline for a ref.",
        "args": {"ref": "HEAD"},
    },
    {
        "name": "read_file",
        "read_only": True,
        "description": "Read a UTF-8 text file inside the workspace.",
        "args": {"path": "relative/path", "max_bytes": 30000},
    },
    {
        "name": "search_text",
        "read_only": True,
        "description": "Search text inside the workspace.",
        "args": {"query": "keyword", "limit": 40},
    },
    {
        "name": "review_diff",
        "read_only": True,
        "description": "Run deterministic review rules over the current diff.",
        "args": {},
    },
    {
        "name": "run_tests",
        "read_only": False,
        "description": "Run the detected or provided low-risk test command.",
        "args": {"timeout": 120},
    },
    {
        "name": "run_shell",
        "read_only": False,
        "description": "Run an explicit shell command after policy checks; medium-risk commands require user approval.",
        "args": {"command": "python3 -m pytest", "timeout": 120},
    },
]

READ_ONLY_TOOLS = {tool["name"] for tool in TOOL_DOCS if tool["read_only"]}


def allowed_tool_names(mode: str) -> set[str]:
    if mode == "review":
        return set(READ_ONLY_TOOLS)
    return {tool["name"] for tool in TOOL_DOCS}


def allowed_tool_docs(mode: str) -> list[dict[str, Any]]:
    names = allowed_tool_names(mode)
    return [tool for tool in TOOL_DOCS if tool["name"] in names]


def choose_rule_step(message: str, mode: str, observations: list[dict[str, Any]], context_tools: list[tuple[str, dict[str, Any]]]) -> AgentStep:
    sequence = [
        ("list_files", {"path": ".", "max_depth": 1, "limit": 40}),
        ("detect_project", {}),
        ("git_status", {}),
        *context_tools,
    ]
    allowed = allowed_tool_names(mode)
    for tool, args in sequence:
        if tool not in allowed:
            continue
        if not observation_seen(observations, tool, args):
            return AgentStep(action="tool", tool=tool, args=args, reason=f"collect context with {tool}", source="rules")
    return AgentStep(action="finish", reason="required context is collected", source="rules")


def build_planner_messages(
    *,
    language: str,
    message: str,
    mode: str,
    workspace: str,
    observations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    language_name = "English" if language.startswith("en") else "中文"
    system = (
        f"你是 aicode 的工具规划器。使用{language_name}思考，但只能输出一个 JSON object。"
        "不要输出 Markdown。不要解释。"
        '格式只能是 {"action":"tool","tool":"read_file","args":{"path":"README.md"},"reason":"..."} '
        '或 {"action":"finish","reason":"..."}。'
        "只能选择 allowed_tools 中的工具。review 模式只允许只读分析。"
    )
    payload = {
        "user_request": message,
        "mode": mode,
        "workspace": workspace,
        "allowed_tools": allowed_tool_docs(mode),
        "observations": observations,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def parse_agent_step(text: str, allowed_tools: set[str]) -> AgentStep | None:
    raw = extract_json_object(text)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    action = str(payload.get("action") or "").strip().lower()
    reason = str(payload.get("reason") or "").strip()
    if action == "finish":
        return AgentStep(action="finish", reason=reason or "planner finished", source="model")
    if action != "tool":
        return None

    tool = str(payload.get("tool") or "").strip()
    if tool not in allowed_tools:
        return None
    args = payload.get("args") or {}
    if not isinstance(args, dict):
        return None
    return AgentStep(action="tool", tool=tool, args=args, reason=reason or f"planner selected {tool}", source="model")


def extract_json_object(text: str) -> str | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end < start:
        return None
    return candidate[start : end + 1]


def observation_seen(observations: list[dict[str, Any]], tool: str, args: dict[str, Any]) -> bool:
    expected = step_key(tool, args)
    for observation in observations:
        if observation.get("step_key") == expected:
            return True
        if "step_key" not in observation and observation.get("tool") == tool:
            return True
    return False


def step_key(tool: str, args: dict[str, Any]) -> str:
    return json.dumps({"tool": tool, "args": args}, ensure_ascii=False, sort_keys=True, default=str)
