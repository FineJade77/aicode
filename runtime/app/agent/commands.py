from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.project.config import load_project_config
from app.agent.types import AgentRequest


MAX_CONTEXT_TOOLS = 6
STOPWORDS = {
    "and",
    "code",
    "file",
    "fix",
    "for",
    "implement",
    "the",
    "update",
    "write",
}


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

    tools: list[tuple[str, dict[str, Any]]] = []
    targets = extract_targets(message, workspace_names)
    for workspace, target in targets:
        tools.append(context_tool_for_target(request.workspace, workspace, target))

    for query in extract_file_queries(message, tools):
        args = {"query": query, "limit": 20}
        if workspace_name:
            args["workspace"] = workspace_name
        tools.append(("find_files", args))

    target_texts = [target for _workspace, target in targets]
    for keyword in extract_keywords(message):
        if keyword in workspace_names:
            continue
        if keyword_mentions_explicit_target(keyword, target_texts):
            continue
        args = {"query": keyword, "limit": 40}
        if workspace_name:
            args["workspace"] = workspace_name
        tools.append(("search_text", args))

    return dedupe_tools(tools)[:MAX_CONTEXT_TOOLS]


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
    targets = extract_targets(message, set())
    for _workspace, target in reversed(targets):
        return target
    return None


def extract_targets(message: str, workspace_names: set[str]) -> list[tuple[str | None, str]]:
    targets: list[tuple[str | None, str]] = []
    seen: set[tuple[str | None, str]] = set()
    for part in message.split():
        cleaned = clean_token(part)
        if not cleaned or "://" in cleaned:
            continue
        scoped = parse_workspace_scoped_target(cleaned, workspace_names)
        if scoped:
            key = (scoped[0], scoped[1])
            if key not in seen:
                targets.append(scoped)
                seen.add(key)
            continue
        target = strip_line_suffix(cleaned)
        if not looks_like_file_token(target):
            continue
        key = (None, target)
        if key not in seen:
            targets.append(key)
            seen.add(key)
    return targets


def context_tool_for_target(workspace: str, workspace_name: str | None, target: str) -> tuple[str, dict[str, Any]]:
    if workspace_name:
        return "read_file", {"path": target, "max_bytes": 30_000, "workspace": workspace_name}
    if "/" in target or (Path(workspace) / target).is_file():
        return "read_file", {"path": target, "max_bytes": 30_000}
    return "find_files", {"query": target, "limit": 20}


def extract_file_queries(message: str, tools: list[tuple[str, dict[str, Any]]]) -> list[str]:
    existing = {str(args.get("query") or args.get("path") or "") for _tool, args in tools}
    queries: list[str] = []
    for token in extract_backtick_tokens(message):
        if looks_like_file_token(token) and token not in existing:
            queries.append(token)
    return queries[:2]


def looks_like_file_token(token: str) -> bool:
    if not token or token.startswith("-") or token.startswith("http"):
        return False
    if token in {".", "..", "./", "../"}:
        return False
    return "/" in token or "." in token


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
        scoped = parse_workspace_scoped_target(cleaned, workspace_names)
        if scoped:
            return scoped
    return None


def parse_workspace_scoped_target(token: str, workspace_names: set[str]) -> tuple[str, str] | None:
    if not workspace_names:
        return None
    workspace_name, separator, target = token.partition(":")
    if not separator or workspace_name not in workspace_names:
        return None
    target = strip_line_suffix(target.strip())
    if not target:
        return None
    return workspace_name, target


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
    keywords = extract_keywords(message)
    return keywords[0] if keywords else None


def extract_keywords(message: str) -> list[str]:
    keywords: list[str] = []
    for token in ["login", "auth", "test", "pytest", "go test", "错误", "失败", "测试", "登录", "认证"]:
        if token in message.lower() or token in message:
            keywords.append(token)
    for token in extract_backtick_tokens(message):
        add_keyword_candidate(keywords, token)
    for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", message):
        add_keyword_candidate(keywords, token)
    return keywords[:2]


def keyword_mentions_explicit_target(keyword: str, targets: list[str]) -> bool:
    lowered = keyword.lower()
    return any(lowered in target.lower() for target in targets)


def add_keyword_candidate(keywords: list[str], token: str) -> None:
    token = token.strip()
    if not token or looks_like_file_token(token):
        return
    if token.lower() in STOPWORDS:
        return
    if token not in keywords:
        keywords.append(token)


def extract_backtick_tokens(message: str) -> list[str]:
    return [clean_token(token) for token in re.findall(r"`([^`]+)`", message)]


def dedupe_tools(tools: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    deduped: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for tool, args in tools:
        key = f"{tool}:{args}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append((tool, args))
    return deduped
