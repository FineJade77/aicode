from __future__ import annotations

from pathlib import PurePosixPath

from app.tools.base import (
    IGNORED_DIRS,
    ToolContext,
    ToolResult,
    is_protected_path,
    is_within_workspace,
    resolve_tool_workspace,
    scoped_display_path,
)

MAX_GLOB_RESULTS = 200
DEFAULT_GLOB_RESULTS = 100


class GlobTool:
    """Find files by path pattern.

    Separate from `search` on purpose: `search` answers "which files contain this
    text", `glob` answers "which files are named like this". Collapsing them
    forces "find the config files" to be expressed as a regex over paths, which
    is both awkward for the model and slower.
    """

    def __init__(self, spec) -> None:
        self.spec = spec

    async def run(self, args: dict, context: ToolContext) -> ToolResult:
        root, workspace_name = resolve_tool_workspace(context, args.get("workspace"))
        pattern = str(args.get("pattern") or "").strip()
        if not pattern:
            return ToolResult(success=False, error="pattern must not be empty")

        rejection = reject_escaping_pattern(pattern)
        if rejection:
            return ToolResult(success=False, error=rejection, risk_level="medium")

        limit = max(1, min(int(args.get("limit") or DEFAULT_GLOB_RESULTS), MAX_GLOB_RESULTS))

        try:
            candidates = sorted(root.glob(pattern))
        except (OSError, ValueError) as exc:
            return ToolResult(success=False, error=f"invalid pattern: {exc}")

        matches: list[str] = []
        truncated = False
        for candidate in candidates:
            if len(matches) >= limit:
                truncated = True
                break
            if not candidate.is_file():
                continue
            # A pattern cannot be trusted to stay inside the workspace even after
            # the syntactic check: a symlink inside the tree can still point out.
            if not is_within_workspace(root, candidate):
                continue
            if any(part in IGNORED_DIRS for part in candidate.parts):
                continue
            relative = candidate.relative_to(root).as_posix()
            if is_protected_path(relative, context.protected_paths):
                continue
            matches.append(scoped_display_path(workspace_name, root, candidate))

        prefix = f"{workspace_name}: " if workspace_name else ""
        if not matches:
            return ToolResult(
                success=True,
                text=f"{prefix}no files match: {pattern}",
                data={"pattern": pattern, "matches": 0, "truncated": False},
            )
        header = f"{prefix}{len(matches)} files match {pattern}"
        if truncated:
            header += f" (truncated at {limit}; narrow the pattern)"
        return ToolResult(
            success=True,
            text=header + "\n" + "\n".join(matches),
            data={"pattern": pattern, "matches": len(matches), "truncated": truncated},
        )


def reject_escaping_pattern(pattern: str) -> str:
    """Reject a pattern that tries to leave the workspace.

    Checked syntactically before globbing, because `Path.glob` on an absolute or
    parent-relative pattern would happily enumerate outside the tree.
    """
    normalized = pattern.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        return "pattern must be relative to the workspace"
    if normalized.startswith("~"):
        return "pattern must not reference a home directory"
    parts = PurePosixPath(normalized).parts
    if ".." in parts:
        return "pattern must not escape the workspace"
    return ""
