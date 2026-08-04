from __future__ import annotations

import inspect
import os
import re
import shlex
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from app.agent.ports import ToolSpec
from app.agent.session import MAX_PLAN_ITEMS, PLAN_ITEM_STATUSES, PlanItem
from app.config import settings
from app.execution.background import BackgroundProcessError
from app.execution.docker import docker_available, missing_image_hint
from app.execution.models import ResourceLimits
from app.execution.sandbox_os import (
    SEATBELT_BINARY,
    build_seatbelt_profile,
    os_sandbox_available,
    unavailable_reason,
)
from app.project.config import load_project_config
from app.security import redact_known_environment_secrets
from app.tools.ask import AskUserTool
from app.tools.base import (
    IGNORED_DIRS,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    display_path,
    is_protected_path,
    is_within_workspace,
    reject_protected_path,
    resolve_tool_workspace,
    resolve_workspace_path,
    scoped_display_path,
)
from app.tools.command import run_command, run_shell_command
from app.tools.edit import file_hash
from app.tools.file import ListFilesTool
from app.tools.glob import DEFAULT_GLOB_RESULTS, MAX_GLOB_RESULTS, GlobTool
from app.tools.hooks import run_hooks
from app.tools.related import RelatedFilesTool
from app.tools.review import ReviewDiffTool

MAX_READ_LINES = 500
DEFAULT_READ_LINES = 200
MAX_SEARCH_RESULTS = 40
DEFAULT_SEARCH_TIMEOUT = 30
DEFAULT_BASH_TIMEOUT = 120
MAX_BASH_TIMEOUT = 600

WORKSPACE_ARG = {"type": "string", "description": "Optional configured read-only workspace name; defaults to the primary workspace"}

# Modes whose tool schema must not include any write tool. Declared once and
# attached to the specs themselves, so a new write tool inherits the restriction
# instead of relying on someone remembering to extend a separate name set.
WRITE_HIDDEN_MODES = frozenset({"review", "explain"})


@dataclass(frozen=True, slots=True)
class FunctionTool:
    """Adapts a plain callable to the Tool protocol."""

    spec: ToolSpec
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self.handler(args, context)


@dataclass(frozen=True, slots=True)
class UpdatePlanTool:
    """Externalises the model's plan into session state.

    The plan serves two purposes: it shows the user what the agent believes it is
    doing during a long task, and it gives the model something to check itself
    against between steps.
    """

    spec: ToolSpec

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.session is None:
            return ToolResult(success=False, error="update_plan requires an active session", risk_level="low")
        raw_items = args.get("items")
        if not isinstance(raw_items, list):
            return ToolResult(success=False, error="items must be an array", risk_level="low")
        items: list[PlanItem] = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                return ToolResult(success=False, error="each plan item must be an object", risk_level="low")
            text = str(entry.get("text") or "").strip()
            status = str(entry.get("status") or "pending")
            if not text:
                return ToolResult(success=False, error="each plan item needs non-empty text", risk_level="low")
            if status not in PLAN_ITEM_STATUSES:
                return ToolResult(
                    success=False,
                    error=f"unknown plan status {status!r}; use pending, in_progress or done",
                    risk_level="low",
                )
            items.append(PlanItem(text=text, status=status))  # type: ignore[arg-type]
        if len(items) > MAX_PLAN_ITEMS:
            return ToolResult(
                success=False,
                error=f"a plan may hold at most {MAX_PLAN_ITEMS} items; keep it to the steps that matter",
                risk_level="low",
            )
        stored = context.session.set_plan(items)
        summary = "\n".join(f"[{item.status}] {item.text}" for item in stored) or "(plan cleared)"
        return ToolResult(
            success=True,
            text=f"Plan updated ({len(stored)} items):\n{summary}",
            data={"items": [item.to_dict() for item in stored]},
        )


@dataclass(frozen=True, slots=True)
class EditFileTool:
    """Registered so `edit_file` has a spec, but never executed from here.

    The Agent Loop intercepts `approval="diff"` tools to present a diff and apply
    the change itself. Reaching this body means that routing was bypassed, so it
    reports that rather than falling through to "unknown tool", which would be
    actively wrong about a tool that does exist.
    """

    spec: ToolSpec

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        del args, context
        return ToolResult(success=False, error="edit_file is applied by the agent loop after diff approval", risk_level="medium")


def context_first(handler: Callable[..., Any]) -> Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]:
    """Wrap a `(context, arguments)` tool function, sync or async.

    The built-in tools predate the registry and come in both argument orders and
    both sync and async flavours; normalising here avoids rewriting them all.
    """

    async def run(args: dict[str, Any], context: ToolContext) -> ToolResult:
        outcome = handler(context, args)
        if inspect.isawaitable(outcome):
            return await outcome
        return outcome

    return run

# One declaration per tool. `read_only` and `approval` live here rather than in
# separate name sets, which is what previously let the registry and the policy
# engine drift apart on which tools are read-only.
TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="read_file",
        description="Read a text file with line numbers. Use offset and limit to continue through large files.",
        read_only=True,
        approval="none",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path"},
                "offset": {"type": "integer", "description": "Starting line number, beginning at 1", "default": 1},
                "limit": {"type": "integer", "description": f"Number of lines; default {DEFAULT_READ_LINES}, maximum {MAX_READ_LINES}"},
                "workspace": WORKSPACE_ARG,
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="search",
        description="Search repository text with a regular expression to locate symbols, strings, or files. Optionally filter with a glob.",
        read_only=True,
        approval="none",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Regular expression"},
                "glob": {"type": "string", "description": "Optional file filter such as *.py or src/**"},
                "limit": {"type": "integer", "default": MAX_SEARCH_RESULTS},
                "workspace": WORKSPACE_ARG,
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="glob",
        description=(
            "Find files by path pattern, for example '**/*.py' or 'src/**/test_*.go'. "
            "Use this to locate files by name; use search to find files by their contents."
        ),
        read_only=True,
        approval="none",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern relative to the workspace root"},
                "limit": {"type": "integer", "description": f"Maximum results; default {DEFAULT_GLOB_RESULTS}, maximum {MAX_GLOB_RESULTS}", "default": DEFAULT_GLOB_RESULTS},
                "workspace": WORKSPACE_ARG,
            },
            "required": ["pattern"],
        },
    ),
    ToolSpec(
        name="list_files",
        description="List directory structure. Use this to understand layout; use search to find specific content.",
        read_only=True,
        approval="none",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "max_depth": {"type": "integer", "default": 2},
                "workspace": WORKSPACE_ARG,
            },
        },
    ),
    ToolSpec(
        name="related_files",
        description="Find read-only context related to a file using source/test naming, matching names, and reference lines.",
        read_only=True,
        approval="none",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path"},
                "limit": {"type": "integer", "default": 20},
                "workspace": WORKSPACE_ARG,
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="bash",
        description=(
            "Run shell commands in the primary workspace. Low-risk commands run directly, medium-risk commands "
            "require approval, and destructive commands are denied. "
            "Set background to true for a command that does not terminate on its own or runs for a long time "
            "(dev server, watch build, long compile): it returns a handle immediately instead of blocking, and you "
            "read its output with read_output and end it with stop_command."
        ),
        read_only=False,
        approval="gate",
        hidden_in_modes=WRITE_HIDDEN_MODES,
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": f"Seconds; default {DEFAULT_BASH_TIMEOUT}", "default": DEFAULT_BASH_TIMEOUT},
                "background": {
                    "type": "boolean",
                    "description": "Run without waiting and return a handle. Use for servers, watchers, and long builds.",
                    "default": False,
                },
            },
            "required": ["command"],
        },
    ),
    ToolSpec(
        name="edit_file",
        description=(
            "Edit files in the primary workspace after the user approves the diff. "
            "For replace, old_text must be an exact, unique source fragment and new_text is its replacement. "
            "To change several places in one file, pass edits instead of old_text/new_text: all replacements are "
            "matched against the current file and applied together under a single approval. "
            "For create, leave old_text empty and provide the complete file in new_text. "
            "For delete, set delete to true."
        ),
        read_only=False,
        approval="diff",
        hidden_in_modes=WRITE_HIDDEN_MODES,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "default": ""},
                "new_text": {"type": "string", "default": ""},
                "edits": {
                    "type": "array",
                    "description": "Several replacements in one file, applied together under one approval. Use instead of old_text/new_text.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {"type": "string", "description": "Exact, unique fragment of the current file"},
                            "new_text": {"type": "string"},
                        },
                        "required": ["old_text", "new_text"],
                    },
                },
                "delete": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="read_output",
        description=(
            "Read new output from a background command started with bash(background=true). "
            "Each call returns only what has arrived since the previous call for that handle. "
            "Call it with no handle to list every background command and its status."
        ),
        read_only=True,
        approval="none",
        hidden_in_modes=WRITE_HIDDEN_MODES,
        input_schema={
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "The handle returned by bash(background=true)."},
            },
        },
    ),
    ToolSpec(
        name="stop_command",
        description=(
            "Stop a background command started with bash(background=true), killing its whole process group. "
            "Stop anything you started once you no longer need it."
        ),
        # Not read-only: it terminates a process. No approval of its own, because
        # stopping something the agent started is strictly de-escalating — the
        # command that created it was already gated.
        read_only=False,
        approval="none",
        hidden_in_modes=WRITE_HIDDEN_MODES,
        input_schema={
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
        },
    ),
    ToolSpec(
        name="ask_user",
        description=(
            "Ask the user a question and wait for their answer. Use this only when the requirement is "
            "genuinely ambiguous and guessing wrong would waste the work — which framework or library to use, "
            "whether to keep an API backward compatible, whether a new dependency is acceptable. "
            "Do not use it for things you can determine by reading the project, and do not ask the same "
            "question twice. Offer options when the choice is between a few known alternatives."
        ),
        # Not read-only: it blocks the turn on a user round trip, so it must never
        # be batched with anything. It touches nothing outside the session, so it
        # needs no approval of its own — the question *is* the interaction.
        read_only=False,
        approval="none",
        hidden_in_modes=WRITE_HIDDEN_MODES,
        input_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question, phrased so it can be answered in one short reply.",
                },
                "options": {
                    "type": "array",
                    "description": "Optional list of concrete choices, when the answer is one of a few known alternatives.",
                    "items": {"type": "string"},
                },
            },
            "required": ["question"],
        },
    ),
    ToolSpec(
        name="update_plan",
        description=(
            "Record or update your plan for a multi-step task. Replace the whole list each time. "
            "Write a plan before starting multi-step work, mark exactly one item in_progress while you work on it, "
            "and mark it done before moving on. Skip this for single-step tasks."
        ),
        # Not read-only: it mutates session state, so it must never be run
        # concurrently with anything. But it touches nothing outside the session,
        # so it needs no approval.
        read_only=False,
        approval="none",
        hidden_in_modes=WRITE_HIDDEN_MODES,
        input_schema={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "The complete plan, in order. Replaces any previous plan.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "What this step accomplishes"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                        },
                        "required": ["text", "status"],
                    },
                }
            },
            "required": ["items"],
        },
    ),
    ToolSpec(
        name="review_diff",
        description="Run deterministic review rules on the current git diff and return structured findings.",
        read_only=True,
        approval="none",
        input_schema={"type": "object", "properties": {}},
    ),
]

# Modes that must never see a write tool, so the model cannot even attempt the
# call. The policy layer still denies non-read-only tools in these modes, in case
# a client bypasses the schema.
NO_TOOL_MODES = frozenset({"commit_message"})

TOOL_SPECS_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}
# Retained for the contract drift tests and any caller that only needs the wire
# form; derived from TOOL_SPECS so it can never disagree with them.
TOOL_SCHEMAS: list[dict[str, Any]] = [spec.to_schema() for spec in TOOL_SPECS]


class ToolRegistry:
    """Name-to-tool lookup with the spec as the single source of truth.

    Replaces a module-level schema list plus an if/elif dispatch chain. Adding a
    tool is now one `register` call instead of edits to the schema list, the
    dispatch chain, and two separate read-only name sets.
    """

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def spec_for(self, name: str) -> ToolSpec | None:
        tool = self._tools.get(name)
        return tool.spec if tool is not None else None

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def schemas_for_mode(self, mode: str) -> list[dict[str, Any]]:
        if mode in NO_TOOL_MODES:
            return []
        return [tool.spec.to_schema() for tool in self._tools.values() if tool.spec.visible_in(mode)]

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> str | None:
        spec = self.spec_for(name)
        if spec is None:
            return None
        return _validate_schema_value(arguments, spec.input_schema, path="")

    async def run(self, name: str, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        started = time.perf_counter()
        validation_error = self.validate_arguments(name, arguments)
        if validation_error is not None:
            return with_duration(
                ToolResult(
                    success=False,
                    error=f"argument validation failed: {validation_error}",
                    data={"validation_error": validation_error},
                ),
                started,
            )
        tool = self._tools.get(name)
        if tool is None:
            return with_duration(ToolResult(success=False, error=f"unknown tool: {name}", risk_level="high"), started)
        try:
            return with_duration(await tool.run(arguments, context), started)
        except ToolError as exc:
            return with_duration(ToolResult(success=False, error=str(exc)), started)
        except Exception as exc:  # Return tool failures to the model without interrupting the loop.
            return with_duration(
                ToolResult(success=False, error=f"{exc.__class__.__name__}: {exc}", risk_level="high"), started
            )


def build_default_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            AskUserTool(TOOL_SPECS_BY_NAME["ask_user"]),
            FunctionTool(TOOL_SPECS_BY_NAME["read_output"], context_first(read_background_output)),
            FunctionTool(TOOL_SPECS_BY_NAME["stop_command"], context_first(stop_background_command)),
            FunctionTool(TOOL_SPECS_BY_NAME["read_file"], context_first(read_file_lines)),
            FunctionTool(TOOL_SPECS_BY_NAME["search"], context_first(run_search)),
            GlobTool(TOOL_SPECS_BY_NAME["glob"]),
            FunctionTool(TOOL_SPECS_BY_NAME["list_files"], ListFilesTool().run),
            FunctionTool(TOOL_SPECS_BY_NAME["related_files"], RelatedFilesTool().run),
            FunctionTool(TOOL_SPECS_BY_NAME["bash"], context_first(run_bash)),
            EditFileTool(TOOL_SPECS_BY_NAME["edit_file"]),
            UpdatePlanTool(TOOL_SPECS_BY_NAME["update_plan"]),
            FunctionTool(TOOL_SPECS_BY_NAME["review_diff"], ReviewDiffTool().run),
        ]
    )



def build_tool_context(
    workspace: str,
    mode: str,
    *,
    execution: Any = None,
    session_id: str = "",
    run_id: str = "",
    trust_level: str = "trusted",
    session: Any = None,
    approvals: Any = None,
    bash_backend: str | None = None,
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
        session=session,
        approvals=approvals,
        # Precedence: this turn's choice, then the project's, then the daemon's.
        # The turn wins because it is the most specific and the most recent
        # thing the user said.
        bash_backend=(
            bash_backend
            or project_config.execution.agent_bash_backend
            or settings.execution.agent_bash_backend
        ),
        hooks=project_config.hooks,
    )


def resolve_bash_backend(configured: str, trust_level: str) -> str:
    """Map the configured policy plus workspace trust onto a concrete backend.

    `auto` resolves to the host, for trusted and untrusted workspaces alike.

    It used to push an untrusted workspace into Docker. That made the sandbox
    the default boundary, and on a machine without a Docker daemon it made
    untrusted workspaces unusable rather than merely unsandboxed — aicode
    refuses to fall back to the host, by design, so the commands simply failed.
    Defaulting to the host trades that boundary for a working tool.

    **What the change does not weaken.** Trust still gates behaviour everywhere
    else it did: the policy engine keeps denying dangerous executables,
    protected paths, workspace escapes and secret literals, and an untrusted
    workspace's project commands still require approval rather than running
    automatically. What is gone by default is process isolation, not the rules.

    `docker` and `os` remain one word away — per session via `/sandbox`, per
    project via `.aicode/config.json`, or daemon-wide via
    `AICODE_AGENT_BASH_BACKEND`.
    """
    if configured in {"host", "docker", "os"}:
        return configured
    return "host"


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
    return DEFAULT_REGISTRY.validate_arguments(name, arguments)


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
    return await DEFAULT_REGISTRY.run(name, arguments, context)


def tool_schemas_for_mode(mode: str) -> list[dict[str, Any]]:
    return DEFAULT_REGISTRY.schemas_for_mode(mode)


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
    data: dict[str, Any] = {"path": label, "total_lines": len(lines), "offset": offset, "shown": len(chunk)}
    if not workspace_name:
        # Only the primary workspace is editable, so only its reads unlock edits
        # — and only its paths are resolvable against the session workspace, which
        # is what makes the hash re-checkable later.
        rel = display_path(root, target)
        content_hash = file_hash(target)
        data["read_path"] = rel
        data["content_hash"] = content_hash
        if context.session is not None:
            context.session.record_read(rel, content_hash)
    return ToolResult(success=True, text=f"{header}\n{shown}", data=data)


async def start_background_command(context: ToolContext, command: str, backend: str) -> ToolResult:
    """Start a command that outlives this tool call.

    Host-only on purpose. The Docker path builds a container per execution and
    tears it down when the call returns, so "background" there would mean
    something materially different from what the model is told it means. Refusing
    is better than quietly running an untrusted workspace's server on the host —
    the same reasoning as the sandbox-unavailable branch above.
    """
    manager = getattr(context.execution, "background", None)
    if manager is None:
        return ToolResult(success=False, error="background commands are not available in this runtime")
    if backend == "docker":
        return ToolResult(
            success=False,
            error=(
                "background commands are not supported on the Docker backend, and this workspace's commands "
                "are routed to the Docker sandbox. Run it in the foreground, or trust the workspace with "
                "`aicode project trust add`."
            ),
            risk_level="high",
            data={"backend": backend, "status": "unsupported"},
        )
    launch, cleanup_paths = _background_launch(context, command, backend)
    if launch is None:
        # Routed to a sandbox that cannot be applied. Starting it unconfined
        # would remove the boundary silently, which is worse than not starting.
        return ToolResult(
            success=False,
            error=(
                f"This command was routed to the OS-level sandbox, but {unavailable_reason()}. "
                "Set execution.agentBashBackend to \"host\", or run it in the foreground."
            ),
            risk_level="high",
            data={"backend": backend, "status": "unavailable"},
        )
    try:
        entry = await manager.start(
            launch,
            workspace=context.workspace,
            session_id=context.session_id,
            cleanup_paths=cleanup_paths,
        )
    except BackgroundProcessError as exc:
        return ToolResult(success=False, error=str(exc), risk_level="medium")
    return ToolResult(
        success=True,
        text=(
            f"Started in the background with handle {entry.handle_id}. "
            f"Read its output with read_output({entry.handle_id}) and end it with stop_command({entry.handle_id})."
        ),
        data={"handle": entry.handle_id, "status": "running", "backend": "host"},
    )


def _background_launch(
    context: ToolContext,
    command: str,
    backend: str,
) -> tuple[str | None, tuple[Path, ...]]:
    """Wrap a background command in the sandbox its backend requires.

    Background commands do not go through `ExecutionRequest`, so the sandboxing
    the foreground path gets for free has to be applied here explicitly —
    otherwise selecting the OS sandbox would silently exempt exactly the
    long-running commands it most needs to cover.
    """
    if backend != "os":
        return command, ()
    if not os_sandbox_available():
        return None, ()
    workspace = context.workspace.expanduser().resolve()
    profile = build_seatbelt_profile(
        writable_paths=(workspace, Path(tempfile.gettempdir()).resolve()),
        masked_paths=tuple(
            path if path.is_absolute() else workspace / path
            for path in (Path(entry) for entry in context.protected_paths)
        ),
        allow_network=True,
    )
    handle, profile_path = tempfile.mkstemp(prefix="aicode-seatbelt-", suffix=".sb")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(profile)
    wrapped = f"{SEATBELT_BINARY} -f {shlex.quote(profile_path)} /bin/sh -c {shlex.quote(command)}"
    return wrapped, (Path(profile_path),)


async def read_background_output(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    manager = getattr(context.execution, "background", None)
    if manager is None:
        return ToolResult(success=False, error="background commands are not available in this runtime")
    handle = str(arguments.get("handle") or "").strip()
    session = context.session
    if not handle:
        entries = manager.list(session_id=context.session_id)
        if not entries:
            return ToolResult(success=True, text="No background commands are running.", data={"commands": []})
        lines = [
            f"- {entry.handle_id}: {entry.status()} · {entry.command}"
            for entry in entries
        ]
        return ToolResult(
            success=True,
            text="Background commands:\n" + "\n".join(lines),
            data={"commands": [entry.describe() for entry in entries]},
        )
    offsets = _background_offsets(session)
    try:
        result = manager.read(handle, after=int(offsets.get(handle, 0)))
    except BackgroundProcessError as exc:
        return ToolResult(success=False, error=str(exc))
    offsets[handle] = result.next_offset

    header = f"{handle} is {result.status}"
    if result.exit_code is not None:
        header += f" (exit={result.exit_code})"
    if result.dropped:
        # Reported rather than hidden: a silent gap would read as contiguous
        # output and could be reasoned about as if nothing were missing.
        header += f"; {result.dropped} characters were dropped because output outran the buffer"
    body = result.text.strip()
    if not body:
        header += "; no new output"
    return ToolResult(
        success=True,
        text=header if not body else f"{header}\n{body}",
        data={
            "handle": handle,
            "status": result.status,
            "exit_code": result.exit_code,
            "dropped_chars": result.dropped,
            "new_chars": len(result.text),
        },
    )


async def stop_background_command(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    manager = getattr(context.execution, "background", None)
    if manager is None:
        return ToolResult(success=False, error="background commands are not available in this runtime")
    handle = str(arguments.get("handle") or "").strip()
    if not handle:
        raise ToolError("handle must not be empty")
    try:
        entry = await manager.stop(handle)
    except BackgroundProcessError as exc:
        return ToolResult(success=False, error=str(exc))
    return ToolResult(
        success=True,
        text=f"{handle} is {entry.status()} (exit={entry.exit_code}).",
        data={"handle": handle, "status": entry.status(), "exit_code": entry.exit_code},
    )


def _background_offsets(session: Any) -> dict[str, int]:
    """Per-session read cursors, so each read returns only what is new.

    Held on the session rather than on the process: two sessions watching one
    command must not consume each other's output.
    """
    if session is None:
        return {}
    offsets = getattr(session, "background_offsets", None)
    if offsets is None:
        offsets = {}
        with suppress(AttributeError):
            session.background_offsets = offsets
    return offsets


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
    if arguments.get("background"):
        return await start_background_command(context, command, backend)
    if backend == "os" and not os_sandbox_available():
        # Same rule as the Docker branch below: a command routed to a sandbox
        # must never fall back to running unconfined, because that turns the
        # boundary into a placebo without telling anyone.
        return ToolResult(
            success=False,
            error=(
                f"This command was routed to the OS-level sandbox, but {unavailable_reason()}. "
                "Set execution.agentBashBackend to \"host\" or \"docker\"."
            ),
            risk_level="high",
            data={"backend": "os", "status": "unavailable", "trust_level": context.trust_level},
        )
    if backend == "docker" and not docker_available():
        # Deliberately no fallback to host execution. Routing an untrusted
        # workspace's command to the host because the sandbox is missing would
        # silently remove the boundary the routing exists to enforce.
        return ToolResult(
            success=False,
            error=(
                "This command was routed to the Docker sandbox, but the Docker CLI is not available. "
                "Start Docker, or run `aicode project trust add` to mark this workspace as trusted, "
                "or set execution.agentBashBackend to \"host\" to accept host execution."
            ),
            risk_level="high",
            data={"backend": "docker", "status": "unavailable", "trust_level": context.trust_level},
        )
    gate = await run_hooks(context, "pre_bash", target=command, values={"command": command})
    if not gate.ok:
        # The hook refused, so the command must not run. Reported as a tool
        # failure rather than an error so the model can react to the lint output
        # instead of the turn ending.
        return ToolResult(
            success=False,
            error=f"blocked by project hook: {gate.reason}\n{gate.output}".strip(),
            risk_level="medium",
            data={"status": "blocked_by_hook", "hook": gate.blocked_by, "hooks_ran": gate.ran},
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


# The built-in set. Module-level so the existing function-style call sites keep
# working; embedders build their own registry instead. Instantiated at the end of
# the module because it references the tool functions defined above.
DEFAULT_REGISTRY = build_default_registry()
