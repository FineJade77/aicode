from __future__ import annotations

from typing import Any

from app.agent.types import AgentRequest


def choose_context_tools(request: AgentRequest) -> list[tuple[str, dict[str, Any]]]:
    message = request.message.strip()
    lowered = message.lower()

    if detect_append_request(message) is not None or detect_replace_request(message) is not None or detect_create_request(message) is not None:
        return []

    shell_command = detect_shell_request(message)
    if shell_command and request.mode != "review":
        return [("run_shell", {"command": shell_command, "timeout": 120})]

    if request.mode == "review" or "审查" in message:
        return [("review_diff", {})]

    if request.mode == "diff" or "diff" in lowered or "变更" in message:
        return [("git_diff", {})]

    if request.mode == "test" or "运行测试" in message or "run tests" in lowered:
        return [("run_tests", {"timeout": 120})]

    target = extract_target(message)
    if target:
        return [("read_file", {"path": target, "max_bytes": 30_000})]

    keyword = extract_keyword(message)
    if keyword:
        return [("search_text", {"query": keyword, "limit": 40})]

    return []


def detect_shell_request(message: str) -> str | None:
    stripped = message.strip()
    lowered = stripped.lower()
    prefixes = ["shell ", "run shell ", "运行命令 ", "执行命令 "]
    for prefix in prefixes:
        if lowered.startswith(prefix) or stripped.startswith(prefix):
            command = stripped[len(prefix) :].strip()
            return command or None
    return None


def detect_append_request(message: str) -> tuple[str, str] | None:
    stripped = message.strip()
    lowered = stripped.lower()
    prefixes = ["append ", "追加 "]
    for prefix in prefixes:
        if lowered.startswith(prefix) or stripped.startswith(prefix):
            body = stripped[len(prefix) :].strip()
            path, text = split_path_and_text(body)
            if path and text:
                return path, text
    return None


def detect_replace_request(message: str) -> tuple[str, str, str] | None:
    stripped = message.strip()
    lowered = stripped.lower()
    prefixes = ["replace ", "替换 "]
    for prefix in prefixes:
        if lowered.startswith(prefix) or stripped.startswith(prefix):
            body = stripped[len(prefix) :].strip()
            path, text = split_path_and_text(body)
            if not path or not text:
                return None
            old_text, new_text = split_replace_text(text)
            if old_text is None or new_text is None:
                return None
            return path, old_text, new_text
    return None


def detect_create_request(message: str) -> tuple[str, str] | None:
    stripped = message.strip()
    lowered = stripped.lower()
    prefixes = ["create ", "创建 "]
    for prefix in prefixes:
        if lowered.startswith(prefix) or stripped.startswith(prefix):
            body = stripped[len(prefix) :].strip()
            path, text = split_path_and_text(body)
            if path and text:
                return path, text
    return None


def split_replace_text(text: str) -> tuple[str | None, str | None]:
    for separator in ["=>", "->"]:
        if separator in text:
            old_text, new_text = text.split(separator, 1)
            old_text = old_text.strip()
            new_text = new_text.strip()
            if old_text and new_text:
                return old_text, new_text
            return None, None
    return None, None


def split_path_and_text(body: str) -> tuple[str | None, str | None]:
    if " " not in body:
        return None, None
    path, text = body.split(" ", 1)
    text = text.strip()
    if not path or not text:
        return None, None
    return path.strip("，。,. "), text


def extract_target(message: str) -> str | None:
    parts = message.split()
    for part in reversed(parts):
        if "/" in part or "." in part:
            cleaned = part.strip("，。,. ")
            if cleaned and not cleaned.startswith("http"):
                return cleaned
    return None


def extract_keyword(message: str) -> str | None:
    for token in ["login", "auth", "test", "pytest", "go test", "错误", "失败", "测试", "登录", "认证"]:
        if token in message.lower() or token in message:
            return token
    return None
