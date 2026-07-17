from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.project.config import load_project_config
from app.agent.types import AgentRequest


def choose_context_tools(request: AgentRequest) -> list[tuple[str, dict[str, Any]]]:
    message = request.message.strip()
    lowered = message.lower()
    workspace_names = configured_workspace_names(request.workspace)
    scoped_target = extract_workspace_scoped_target(message, workspace_names)
    workspace_name = (scoped_target[0] if scoped_target else None) or extract_workspace_name(message, workspace_names)

    if detect_append_request(message) is not None or detect_replace_request(message) is not None or detect_create_request(message) is not None:
        return []

    shell_command = detect_shell_request(message)
    if shell_command and request.mode != "review":
        return [("run_shell", {"command": shell_command, "timeout": 120})]

    if request.mode == "review" or "审查" in message:
        return [("review_diff", {})]

    if request.mode == "diff" or "diff" in lowered or "变更" in message:
        args: dict[str, Any] = {}
        if workspace_name:
            args["workspace"] = workspace_name
        if scoped_target and scoped_target[1] not in {"", "."}:
            args["path"] = scoped_target[1]
        return [("git_diff", args)]

    if request.mode == "test" or "运行测试" in message or "run tests" in lowered:
        return [("run_tests", {"timeout": 120})]

    target = scoped_target[1] if scoped_target else extract_target(message)
    if target:
        args = {"path": target, "max_bytes": 30_000}
        if scoped_target:
            args["workspace"] = scoped_target[0]
        return [("read_file", args)]

    keyword = extract_keyword(message)
    if keyword:
        args = {"query": keyword, "limit": 40}
        if workspace_name:
            args["workspace"] = workspace_name
        return [("search_text", args)]

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


def configured_workspace_names(workspace: str) -> set[str]:
    config = load_project_config(Path(workspace))
    return {ref.name for ref in config.workspaces if ref.mode == "read_only"}


def extract_workspace_scoped_target(message: str, workspace_names: set[str]) -> tuple[str, str] | None:
    if not workspace_names:
        return None
    for part in reversed(message.split()):
        cleaned = clean_token(part)
        if "://" in cleaned:
            continue
        workspace_name, separator, target = cleaned.partition(":")
        if not separator or workspace_name not in workspace_names:
            continue
        target = strip_line_suffix(target.strip())
        if target:
            return workspace_name, target
    return None


def extract_workspace_name(message: str, workspace_names: set[str]) -> str | None:
    if not workspace_names:
        return None
    for part in message.split():
        cleaned = clean_token(part)
        if cleaned in workspace_names:
            return cleaned
        for prefix in ["workspace=", "workspace:", "工作区=", "工作区:"]:
            if cleaned.startswith(prefix) and cleaned[len(prefix) :] in workspace_names:
                return cleaned[len(prefix) :]
    return None


def clean_token(token: str) -> str:
    return token.strip(" \t\r\n，。,.()[]{}'\"`")


def strip_line_suffix(target: str) -> str:
    return re.sub(r":\d+$", "", target)


def extract_keyword(message: str) -> str | None:
    for token in ["login", "auth", "test", "pytest", "go test", "错误", "失败", "测试", "登录", "认证"]:
        if token in message.lower() or token in message:
            return token
    return None
