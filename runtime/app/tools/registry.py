from __future__ import annotations

import re
import shutil
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from app.project.config import load_project_config
from app.tools.base import (
    IGNORED_DIRS,
    ToolContext,
    ToolError,
    ToolResult,
    is_protected_path,
    reject_protected_path,
    resolve_tool_workspace,
    resolve_workspace_path,
    scoped_display_path,
)
from app.tools.command import run_command, run_shell_command
from app.tools.file import ListFilesTool
from app.tools.review import ReviewDiffTool

MAX_READ_LINES = 500
DEFAULT_READ_LINES = 200
MAX_SEARCH_RESULTS = 40
DEFAULT_SEARCH_TIMEOUT = 30
DEFAULT_BASH_TIMEOUT = 120
MAX_BASH_TIMEOUT = 600

WORKSPACE_ARG = {"type": "string", "description": "可选：配置的只读 workspace 名称，默认主 workspace"}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "读取文本文件，按行号返回。文件较大时用 offset/limit 分段继续读。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径"},
                "offset": {"type": "integer", "description": "起始行号，从 1 开始", "default": 1},
                "limit": {"type": "integer", "description": f"读取行数，默认 {DEFAULT_READ_LINES}，最大 {MAX_READ_LINES}"},
                "workspace": WORKSPACE_ARG,
            },
            "required": ["path"],
        },
    },
    {
        "name": "search",
        "description": "在代码库中用正则搜索文本，定位符号、字符串或文件；query 为正则表达式，可用 glob 过滤。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "正则表达式"},
                "glob": {"type": "string", "description": "可选：文件过滤，如 *.py 或 src/**"},
                "limit": {"type": "integer", "default": MAX_SEARCH_RESULTS},
                "workspace": WORKSPACE_ARG,
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_files",
        "description": "列出目录结构。只在需要了解目录布局时使用，找具体内容用 search。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "max_depth": {"type": "integer", "default": 2},
                "workspace": WORKSPACE_ARG,
            },
        },
    },
    {
        "name": "bash",
        "description": "在主 workspace 执行 shell 命令（git、测试、构建等）。低风险命令直接执行；中风险需要用户确认；破坏性命令会被拒绝。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": f"秒，默认 {DEFAULT_BASH_TIMEOUT}", "default": DEFAULT_BASH_TIMEOUT},
            },
            "required": ["command"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "编辑主 workspace 的文件，用户确认 diff 后生效。"
            "replace：old_text 必须是文件中完整且唯一的原文片段，new_text 为替换内容；"
            "create：文件不存在时 old_text 留空、new_text 为完整内容；"
            "delete：delete 参数设为 true。每次编辑独立确认。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "default": ""},
                "new_text": {"type": "string", "default": ""},
                "delete": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
    },
    {
        "name": "review_diff",
        "description": "对当前 git diff 运行确定性 review 规则，输出结构化 finding。",
        "input_schema": {"type": "object", "properties": {}},
    },
]

READ_ONLY_TOOL_NAMES = {"read_file", "search", "list_files", "review_diff"}
TOOL_SCHEMAS_BY_NAME = {schema["name"]: schema for schema in TOOL_SCHEMAS}


def tool_schemas_for_mode(mode: str) -> list[dict[str, Any]]:
    if mode == "review":
        return [schema for schema in TOOL_SCHEMAS if schema["name"] in READ_ONLY_TOOL_NAMES]
    return list(TOOL_SCHEMAS)


def build_tool_context(workspace: str, mode: str, language: str) -> ToolContext:
    project_config = load_project_config(Path(workspace))
    return ToolContext(
        workspace=Path(workspace),
        mode=mode,
        language=language,
        protected_paths=project_config.protected_paths,
        workspace_refs=project_config.workspaces,
        review_disabled_rules=project_config.review.disabled_rules,
        review_large_diff_threshold=project_config.review.large_diff_threshold,
        review_max_findings=project_config.review.max_findings,
    )


def validate_tool_arguments(name: str, arguments: dict[str, Any]) -> str | None:
    schema = TOOL_SCHEMAS_BY_NAME.get(name)
    if schema is None:
        return None
    return _validate_schema_value(arguments, schema["input_schema"], path="")


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str) -> str | None:
    expected_type = schema.get("type")
    label = path or "参数"

    if expected_type == "object":
        if not isinstance(value, dict):
            return f"{label} 应为 object"
        for key in schema.get("required") or []:
            if key not in value:
                return f"缺少必填字段: {key}"
        properties = schema.get("properties") or {}
        for key, item in value.items():
            property_schema = properties.get(key)
            if property_schema is None:
                continue
            error = _validate_schema_value(item, property_schema, path=key if not path else f"{path}.{key}")
            if error is not None:
                return error
        return None

    if expected_type == "string" and not isinstance(value, str):
        return f"{label} 应为 string"
    if expected_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return f"{label} 应为 integer"
    if expected_type == "boolean" and not isinstance(value, bool):
        return f"{label} 应为 boolean"
    return None


async def run_tool(name: str, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    started = time.perf_counter()
    validation_error = validate_tool_arguments(name, arguments)
    if validation_error is not None:
        return with_duration(
            ToolResult(success=False, error=f"参数校验失败: {validation_error}", data={"validation_error": validation_error}),
            started,
        )

    try:
        if name == "read_file":
            return with_duration(read_file_lines(context, arguments), started)
        if name == "search":
            return with_duration(await run_search(context, arguments), started)
        if name == "list_files":
            return with_duration(await ListFilesTool().run(arguments, context), started)
        if name == "review_diff":
            return with_duration(await ReviewDiffTool().run(arguments, context), started)
        if name == "bash":
            return with_duration(await run_bash(context, arguments), started)
        if name == "edit_file":
            return with_duration(ToolResult(success=False, error="edit_file 由 agent loop 单独处理", risk_level="medium"), started)
        return with_duration(ToolResult(success=False, error=f"未知工具: {name}", risk_level="high"), started)
    except ToolError as exc:
        return with_duration(ToolResult(success=False, error=str(exc)), started)
    except Exception as exc:  # 工具异常回给模型，不中断循环
        return with_duration(ToolResult(success=False, error=f"{exc.__class__.__name__}: {exc}", risk_level="high"), started)


def with_duration(result: ToolResult, started: float) -> ToolResult:
    result.duration_ms = max(0, int((time.perf_counter() - started) * 1000))
    return result


def read_file_lines(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    root, workspace_name = resolve_tool_workspace(context, arguments.get("workspace"))
    target = resolve_workspace_path(root, str(arguments.get("path") or ""))
    reject_protected_path(root, target, context.protected_paths)
    if not target.is_file():
        raise ToolError(f"文件不存在: {arguments.get('path')}")
    offset = max(1, int(arguments.get("offset") or 1))
    limit = max(1, min(int(arguments.get("limit") or DEFAULT_READ_LINES), MAX_READ_LINES))
    lines = target.read_text("utf-8", errors="replace").splitlines()
    if lines and offset > len(lines):
        raise ToolError(f"offset {offset} 超出文件行数 {len(lines)}")
    chunk = lines[offset - 1 : offset - 1 + limit]
    shown = "\n".join(f"{offset + index}\t{line}" for index, line in enumerate(chunk))
    label = scoped_display_path(workspace_name, root, target)
    end = offset + len(chunk) - 1
    header = f"{label} 共 {len(lines)} 行，显示第 {offset}-{end} 行"
    if end < len(lines):
        header += f"（未完，可用 offset={end + 1} 继续读）"
    return ToolResult(success=True, text=f"{header}\n{shown}", data={"path": label, "total_lines": len(lines), "offset": offset, "shown": len(chunk)})


async def run_search(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    root, workspace_name = resolve_tool_workspace(context, arguments.get("workspace"))
    query = str(arguments.get("query") or "")
    if not query:
        raise ToolError("query 不能为空")
    limit = max(1, min(int(arguments.get("limit") or MAX_SEARCH_RESULTS), MAX_SEARCH_RESULTS))
    glob_pattern = str(arguments.get("glob") or "")

    try:
        pattern = re.compile(query)
    except re.error as exc:
        raise ToolError(f"正则表达式无效: {exc}")

    if shutil.which("rg"):
        return await _search_with_rg(context, root, workspace_name, query, glob_pattern, limit)
    return _search_with_python(context, root, workspace_name, pattern, query, glob_pattern, limit)


async def _search_with_rg(
    context: ToolContext,
    root: Path,
    workspace_name: str,
    query: str,
    glob_pattern: str,
    limit: int,
) -> ToolResult:
    command = [
        "rg",
        "--line-number",
        "--no-heading",
        "--max-count",
        "5",
        "--hidden",
        "--glob",
        "!.git",
        "--glob",
        "!node_modules",
        "--glob",
        "!.venv",
        "--glob",
        "!__pycache__",
    ]
    if glob_pattern:
        command.extend(["--glob", glob_pattern])
    command.extend(["-e", query])

    proc = await run_command(command, cwd=root, timeout=DEFAULT_SEARCH_TIMEOUT)
    returncode = proc.returncode if proc.returncode is not None else -1

    prefix = f"{workspace_name}: " if workspace_name else ""
    if proc.timed_out:
        raise ToolError(f"搜索超时（{DEFAULT_SEARCH_TIMEOUT}s）: {query}")
    if returncode == 1:
        return ToolResult(success=True, text=f"{prefix}没有匹配: {query}", data={"query": query, "matches": 0})
    if returncode != 0:
        stderr_text = proc.stderr.strip()
        raise ToolError(stderr_text or "rg 执行失败")

    matches: list[str] = []
    for line in proc.stdout.splitlines():
        if len(matches) >= limit:
            break
        rel_path = line.split(":", 1)[0].replace("\\", "/")
        if is_protected_path(rel_path, context.protected_paths):
            continue
        matches.append(line)

    if not matches:
        return ToolResult(success=True, text=f"{prefix}没有匹配: {query}", data={"query": query, "matches": 0})
    return ToolResult(
        success=True,
        text=f"{prefix}匹配 {len(matches)} 处:\n" + "\n".join(matches),
        data={"query": query, "matches": len(matches)},
    )


def _search_with_python(
    context: ToolContext,
    root: Path,
    workspace_name: str,
    pattern: re.Pattern[str],
    query: str,
    glob_pattern: str,
    limit: int,
) -> ToolResult:
    matches: list[str] = []
    for file_path in root.rglob("*"):
        if len(matches) >= limit:
            break
        if not file_path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in file_path.parts):
            continue

        rel_path_str = str(file_path.relative_to(root)).replace("\\", "/")

        # Check glob pattern
        if glob_pattern and not fnmatch(rel_path_str, glob_pattern):
            continue

        # Check protected paths
        if is_protected_path(rel_path_str, context.protected_paths):
            continue

        # Search in file
        try:
            content = file_path.read_text("utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue

        for line_num, line in enumerate(content.splitlines(), start=1):
            if pattern.search(line):
                matches.append(f"{rel_path_str}:{line_num}:{line}")
                if len(matches) >= limit:
                    break

    prefix = f"{workspace_name}: " if workspace_name else ""
    if not matches:
        return ToolResult(success=True, text=f"{prefix}没有匹配: {query}", data={"query": query, "matches": 0})
    return ToolResult(success=True, text=f"{prefix}匹配 {len(matches)} 处:\n" + "\n".join(matches), data={"query": query, "matches": len(matches)})


async def run_bash(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    command = str(arguments.get("command") or "").strip()
    if not command:
        raise ToolError("command 不能为空")
    timeout = max(1, min(int(arguments.get("timeout") or DEFAULT_BASH_TIMEOUT), MAX_BASH_TIMEOUT))
    result = await run_shell_command(command, cwd=context.workspace, timeout=timeout, stderr_to_stdout=True)
    if result.timed_out:
        return ToolResult(
            success=False,
            error=f"命令超时（{timeout}s）: {command}",
            risk_level="medium",
            data={"command": command, "exit_code": result.returncode, "timed_out": True},
        )
    text = f"exit={result.returncode}\n{result.stdout}".rstrip()
    return ToolResult(
        success=result.returncode == 0,
        text=text,
        error="" if result.returncode == 0 else text,
        data={"command": command, "exit_code": result.returncode},
    )
