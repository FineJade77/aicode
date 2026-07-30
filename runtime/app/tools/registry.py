from __future__ import annotations

import os
import re
import shutil
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from app.config.settings import settings
from app.execution.docker import docker_available, missing_image_hint
from app.execution.models import ResourceLimits
from app.project.config import load_project_config
from app.security.secrets import redact_known_environment_secrets
from app.tools.base import (
    IGNORED_DIRS,
    ToolContext,
    ToolError,
    ToolResult,
    is_protected_path,
    is_within_workspace,
    reject_protected_path,
    resolve_tool_workspace,
    resolve_workspace_path,
    scoped_display_path,
)
from app.tools.command import run_command, run_shell_command
from app.tools.file import ListFilesTool
from app.tools.related import RelatedFilesTool
from app.tools.review import ReviewDiffTool

MAX_READ_LINES = 500
DEFAULT_READ_LINES = 200
MAX_SEARCH_RESULTS = 40
DEFAULT_SEARCH_TIMEOUT = 30
DEFAULT_BASH_TIMEOUT = 120
MAX_BASH_TIMEOUT = 600

WORKSPACE_ARG = {"type": "string", "description": "Optional configured read-only workspace name; defaults to the primary workspace"}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a text file with line numbers. Use offset and limit to continue through large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path"},
                "offset": {"type": "integer", "description": "Starting line number, beginning at 1", "default": 1},
                "limit": {"type": "integer", "description": f"Number of lines; default {DEFAULT_READ_LINES}, maximum {MAX_READ_LINES}"},
                "workspace": WORKSPACE_ARG,
            },
            "required": ["path"],
        },
    },
    {
        "name": "search",
        "description": "Search repository text with a regular expression to locate symbols, strings, or files. Optionally filter with a glob.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Regular expression"},
                "glob": {"type": "string", "description": "Optional file filter such as *.py or src/**"},
                "limit": {"type": "integer", "default": MAX_SEARCH_RESULTS},
                "workspace": WORKSPACE_ARG,
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_files",
        "description": "List directory structure. Use this to understand layout; use search to find specific content.",
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
        "name": "related_files",
        "description": "Find read-only context related to a file using source/test naming, matching names, and reference lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path"},
                "limit": {"type": "integer", "default": 20},
                "workspace": WORKSPACE_ARG,
            },
            "required": ["path"],
        },
    },
    {
        "name": "bash",
        "description": "Run shell commands in the primary workspace. Low-risk commands run directly, medium-risk commands require approval, and destructive commands are denied.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": f"Seconds; default {DEFAULT_BASH_TIMEOUT}", "default": DEFAULT_BASH_TIMEOUT},
            },
            "required": ["command"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Edit files in the primary workspace after the user approves the diff. "
            "For replace, old_text must be an exact, unique source fragment and new_text is its replacement. "
            "For create, leave old_text empty and provide the complete file in new_text. "
            "For delete, set delete to true. Each edit is approved independently."
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
        "description": "Run deterministic review rules on the current git diff and return structured findings.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

READ_ONLY_TOOL_NAMES = {"read_file", "search", "list_files", "related_files", "review_diff"}
NO_TOOL_MODES = {"commit_message"}
# Modes that must never see bash/edit_file in their tool schema, so the model
# cannot even attempt a write call. Kept in sync with policy.engine.READ_ONLY_MODES,
# which is the hard enforcement layer in case a client bypasses the schema.
READ_ONLY_SCHEMA_MODES = {"review", "explain"}
TOOL_SCHEMAS_BY_NAME = {schema["name"]: schema for schema in TOOL_SCHEMAS}


def tool_schemas_for_mode(mode: str) -> list[dict[str, Any]]:
    if mode in NO_TOOL_MODES:
        return []
    if mode in READ_ONLY_SCHEMA_MODES:
        return [schema for schema in TOOL_SCHEMAS if schema["name"] in READ_ONLY_TOOL_NAMES]
    return list(TOOL_SCHEMAS)


def build_tool_context(
    workspace: str,
    mode: str,
    *,
    execution: Any = None,
    session_id: str = "",
    run_id: str = "",
    trust_level: str = "trusted",
) -> ToolContext:
    project_config = load_project_config(Path(workspace))
    return ToolContext(
        workspace=Path(workspace),
        mode=mode,
        protected_paths=project_config.protected_paths,
        workspace_refs=project_config.workspaces,
        review_disabled_rules=project_config.review.disabled_rules,
        review_large_diff_threshold=project_config.review.large_diff_threshold,
        review_max_findings=project_config.review.max_findings,
        execution=execution,
        session_id=session_id,
        run_id=run_id,
        trust_level=trust_level,
        bash_backend=project_config.execution.agent_bash_backend or settings.execution.agent_bash_backend,
    )


def resolve_bash_backend(configured: str, trust_level: str) -> str:
    """Map the configured policy plus workspace trust onto a concrete backend.

    `auto` is the default: a workspace the user explicitly trusted runs on the
    host with the full local toolchain, while anything else is pushed into the
    Docker sandbox. `host` and `docker` are escape hatches that ignore trust.
    """
    if configured == "host":
        return "host"
    if configured == "docker":
        return "docker"
    return "host" if trust_level == "trusted" else "docker"


def sandbox_resource_limits(timeout: float) -> ResourceLimits:
    """Resource caps for sandboxed Agent commands.

    Mirrors the knobs `ExecutionApplicationService` already uses for
    test/build/lint so both sandbox entry points stay capped the same way.
    """
    return ResourceLimits(
        timeout_seconds=timeout,
        cpus=os.getenv("AICODE_SANDBOX_CPUS", "2"),
        memory=os.getenv("AICODE_SANDBOX_MEMORY", "2g"),
        pids_limit=_sandbox_pids_limit(),
    )


def _sandbox_pids_limit() -> int:
    try:
        value = int(os.getenv("AICODE_SANDBOX_PIDS_LIMIT", "256"))
    except ValueError:
        return 256
    return value if 1 <= value <= 65535 else 256


def validate_tool_arguments(name: str, arguments: dict[str, Any]) -> str | None:
    schema = TOOL_SCHEMAS_BY_NAME.get(name)
    if schema is None:
        return None
    return _validate_schema_value(arguments, schema["input_schema"], path="")


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str) -> str | None:
    expected_type = schema.get("type")
    label = path or "arguments"

    if expected_type == "object":
        if not isinstance(value, dict):
            return f"{label} must be an object"
        for key in schema.get("required") or []:
            if key not in value:
                return f"missing required field: {key}"
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
        return f"{label} must be a string"
    if expected_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return f"{label} must be an integer"
    if expected_type == "boolean" and not isinstance(value, bool):
        return f"{label} must be a boolean"
    return None


async def run_tool(name: str, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    started = time.perf_counter()
    validation_error = validate_tool_arguments(name, arguments)
    if validation_error is not None:
        return with_duration(
            ToolResult(success=False, error=f"argument validation failed: {validation_error}", data={"validation_error": validation_error}),
            started,
        )

    try:
        if name == "read_file":
            return with_duration(read_file_lines(context, arguments), started)
        if name == "search":
            return with_duration(await run_search(context, arguments), started)
        if name == "list_files":
            return with_duration(await ListFilesTool().run(arguments, context), started)
        if name == "related_files":
            return with_duration(await RelatedFilesTool().run(arguments, context), started)
        if name == "review_diff":
            return with_duration(await ReviewDiffTool().run(arguments, context), started)
        if name == "bash":
            return with_duration(await run_bash(context, arguments), started)
        if name == "edit_file":
            return with_duration(ToolResult(success=False, error="edit_file is handled by the agent loop", risk_level="medium"), started)
        return with_duration(ToolResult(success=False, error=f"unknown tool: {name}", risk_level="high"), started)
    except ToolError as exc:
        return with_duration(ToolResult(success=False, error=str(exc)), started)
    except Exception as exc:  # Return tool failures to the model without interrupting the loop.
        return with_duration(ToolResult(success=False, error=f"{exc.__class__.__name__}: {exc}", risk_level="high"), started)


def with_duration(result: ToolResult, started: float) -> ToolResult:
    result.text = redact_known_environment_secrets(result.text)
    result.error = redact_known_environment_secrets(result.error)
    result.data = redact_known_environment_secrets(result.data)
    result.duration_ms = max(0, int((time.perf_counter() - started) * 1000))
    return result


def read_file_lines(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    root, workspace_name = resolve_tool_workspace(context, arguments.get("workspace"))
    target = resolve_workspace_path(root, str(arguments.get("path") or ""))
    reject_protected_path(root, target, context.protected_paths)
    if not target.is_file():
        raise ToolError(f"file does not exist: {arguments.get('path')}")
    offset = max(1, int(arguments.get("offset") or 1))
    limit = max(1, min(int(arguments.get("limit") or DEFAULT_READ_LINES), MAX_READ_LINES))
    lines = target.read_text("utf-8", errors="replace").splitlines()
    if lines and offset > len(lines):
        raise ToolError(f"offset {offset} exceeds the file's {len(lines)} lines")
    chunk = lines[offset - 1 : offset - 1 + limit]
    shown = "\n".join(f"{offset + index}\t{line}" for index, line in enumerate(chunk))
    label = scoped_display_path(workspace_name, root, target)
    end = offset + len(chunk) - 1
    header = f"{label} has {len(lines)} lines; showing {offset}-{end}"
    if end < len(lines):
        header += f" (more available; continue with offset={end + 1})"
    return ToolResult(success=True, text=f"{header}\n{shown}", data={"path": label, "total_lines": len(lines), "offset": offset, "shown": len(chunk)})


async def run_search(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    root, workspace_name = resolve_tool_workspace(context, arguments.get("workspace"))
    query = str(arguments.get("query") or "")
    if not query:
        raise ToolError("query must not be empty")
    limit = max(1, min(int(arguments.get("limit") or MAX_SEARCH_RESULTS), MAX_SEARCH_RESULTS))
    glob_pattern = str(arguments.get("glob") or "")

    try:
        pattern = re.compile(query)
    except re.error as exc:
        raise ToolError(f"invalid regular expression: {exc}") from exc

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
    for protected_path in context.protected_paths:
        command.extend(["--glob", f"!{protected_path}"])
    command.extend(["-e", query])

    proc = await run_command(
        command,
        cwd=root,
        timeout=DEFAULT_SEARCH_TIMEOUT,
        execution=context.execution,
        metadata={
            "action": "tool.search",
            "mode": context.mode,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "tool_call_id": context.tool_call_id,
            "masked_paths": context.protected_paths,
            "trust_level": context.trust_level,
        },
    )
    returncode = proc.returncode if proc.returncode is not None else -1

    prefix = f"{workspace_name}: " if workspace_name else ""
    if proc.timed_out:
        raise ToolError(f"search timed out ({DEFAULT_SEARCH_TIMEOUT}s): {query}")
    if returncode == 1:
        return ToolResult(success=True, text=f"{prefix}no matches: {query}", data={"query": query, "matches": 0})
    if returncode != 0:
        stderr_text = proc.stderr.strip()
        raise ToolError(stderr_text or "rg failed")

    matches: list[str] = []
    for line in proc.stdout.splitlines():
        if len(matches) >= limit:
            break
        rel_path = line.split(":", 1)[0].replace("\\", "/")
        if is_protected_path(rel_path, context.protected_paths):
            continue
        matches.append(line)

    if not matches:
        return ToolResult(success=True, text=f"{prefix}no matches: {query}", data={"query": query, "matches": 0})
    return ToolResult(
        success=True,
        text=f"{prefix}{len(matches)} matches:\n" + "\n".join(matches),
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
        if not is_within_workspace(root, file_path):
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
        return ToolResult(success=True, text=f"{prefix}no matches: {query}", data={"query": query, "matches": 0})
    return ToolResult(success=True, text=f"{prefix}{len(matches)} matches:\n" + "\n".join(matches), data={"query": query, "matches": len(matches)})


async def run_bash(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    command = str(arguments.get("command") or "").strip()
    if not command:
        raise ToolError("command must not be empty")
    timeout = max(1, min(int(arguments.get("timeout") or DEFAULT_BASH_TIMEOUT), MAX_BASH_TIMEOUT))
    backend = resolve_bash_backend(context.bash_backend, context.trust_level)
    if backend == "docker" and not docker_available():
        # Deliberately no fallback to host execution. Routing an untrusted
        # workspace's command to the host because the sandbox is missing would
        # silently remove the boundary the routing exists to enforce.
        return ToolResult(
            success=False,
            error=(
                "This command was routed to the Docker sandbox, but the Docker CLI is not available. "
                "Start Docker, or run `aicode trust add` to mark this workspace as trusted, "
                "or set execution.agentBashBackend to \"host\" to accept host execution."
            ),
            risk_level="high",
            data={"backend": "docker", "status": "unavailable", "trust_level": context.trust_level},
        )
    result = await run_shell_command(
        command,
        cwd=context.workspace,
        timeout=timeout,
        stderr_to_stdout=True,
        execution=context.execution,
        backend=backend,
        limits=sandbox_resource_limits(timeout) if backend == "docker" else None,
        metadata={
            "action": "agent.bash",
            "mode": context.mode,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "tool_call_id": context.tool_call_id,
            "masked_paths": context.protected_paths,
            "trust_level": context.trust_level,
        },
    )
    data = {
        "execution_id": result.execution_id,
        "backend": result.backend,
        "status": result.status,
        "exit_code": result.returncode,
        "timed_out": result.timed_out,
    }
    if result.timed_out:
        return ToolResult(
            success=False,
            error=f"command timed out ({timeout}s)",
            risk_level="medium",
            data=data,
        )
    if backend == "docker" and result.returncode != 0:
        hint = missing_image_hint(command, result.combined_output)
        if hint:
            return ToolResult(success=False, error=hint, risk_level="medium", data={**data, "status": "image_missing"})
    text = f"exit={result.returncode}\n{result.stdout}".rstrip()
    return ToolResult(
        success=result.returncode == 0,
        text=text,
        error="" if result.returncode == 0 else text,
        data=data,
    )
