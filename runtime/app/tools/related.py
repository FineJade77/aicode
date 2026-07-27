from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.tools.base import (
    IGNORED_DIRS,
    ToolContext,
    ToolError,
    ToolResult,
    display_path,
    is_within_workspace,
    is_protected_path,
    reject_protected_path,
    resolve_tool_workspace,
    resolve_workspace_path,
    scoped_display_path,
)

DEFAULT_RELATED_LIMIT = 20
MAX_RELATED_LIMIT = 50
MAX_SCAN_FILES = 5_000
MAX_REFERENCE_FILE_BYTES = 512_000

RELATED_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".json",
    ".kt",
    ".md",
    ".mjs",
    ".py",
    ".rs",
    ".scss",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".vue",
    ".yaml",
    ".yml",
}

TEST_DIR_NAMES = {"test", "tests", "spec", "specs", "__tests__"}
TEST_MARKERS = ("test_", "_test", ".test", "-test", "spec_", "_spec", ".spec", "-spec")


@dataclass
class RelatedCandidate:
    path: Path
    reasons: list[str] = field(default_factory=list)
    score: int = 0
    line: int | None = None
    snippet: str = ""
    match: str = ""


class RelatedFilesTool:
    name = "related_files"

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        root, workspace_name = resolve_tool_workspace(context, args.get("workspace"))
        raw_path = str(args.get("path") or "")
        if not raw_path:
            raise ToolError("path must not be empty")
        target = resolve_workspace_path(root, raw_path)
        reject_protected_path(root, target, context.protected_paths)
        if not target.is_file():
            raise ToolError(f"file does not exist: {raw_path}")

        limit = max(1, min(int(args.get("limit") or DEFAULT_RELATED_LIMIT), MAX_RELATED_LIMIT))
        related = find_related_files(root, target, context.protected_paths, limit=limit)
        target_label = scoped_display_path(workspace_name, root, target)
        if not related:
            return ToolResult(
                success=True,
                text=f"No clearly related files found for {target_label}",
                data={"path": target_label, "related": [], "workspace": workspace_name or "main"},
            )

        rows: list[str] = []
        payload: list[dict[str, Any]] = []
        for candidate in related:
            label = scoped_display_path(workspace_name, root, candidate.path)
            suffix = ""
            if candidate.line is not None:
                suffix = f":{candidate.line}"
            reason = ",".join(candidate.reasons)
            line = f"- {label}{suffix} [{reason}]"
            if candidate.snippet:
                line += f" {candidate.snippet}"
            rows.append(line)
            item = {
                "path": label,
                "reasons": candidate.reasons,
                "score": candidate.score,
            }
            if candidate.line is not None:
                item["line"] = candidate.line
                item["snippet"] = candidate.snippet
                item["match"] = candidate.match
            payload.append(item)

        text = f"{len(related)} related files for {target_label}:\n" + "\n".join(rows)
        return ToolResult(
            success=True,
            text=text,
            data={"path": target_label, "related": payload, "workspace": workspace_name or "main"},
        )


def find_related_files(root: Path, target: Path, protected_paths: list[str], limit: int) -> list[RelatedCandidate]:
    target = target.resolve()
    candidates: dict[Path, RelatedCandidate] = {}
    target_key = normalized_stem(target)
    target_is_test = is_test_file(root, target)
    reference_tokens = reference_search_tokens(root, target)
    files = iter_related_files(root, protected_paths)

    for file_path in files:
        file_path = file_path.resolve()
        if file_path == target:
            continue
        file_key = normalized_stem(file_path)
        if file_key and file_key == target_key:
            if is_test_file(root, file_path) != target_is_test:
                add_candidate(candidates, file_path, "source_test_pair", 100)
            else:
                add_candidate(candidates, file_path, "same_name", 70)

    for file_path in files:
        file_path = file_path.resolve()
        if file_path == target or (file_path in candidates and candidates[file_path].score >= 100):
            continue
        reference = first_reference(file_path, reference_tokens)
        if reference is None:
            continue
        line, snippet, match = reference
        add_candidate(candidates, file_path, "reference", 60, line=line, snippet=snippet, match=match)

    return sorted(candidates.values(), key=lambda item: (-item.score, display_path(root, item.path)))[:limit]


def add_candidate(
    candidates: dict[Path, RelatedCandidate],
    path: Path,
    reason: str,
    score: int,
    *,
    line: int | None = None,
    snippet: str = "",
    match: str = "",
) -> None:
    candidate = candidates.setdefault(path, RelatedCandidate(path=path))
    if reason not in candidate.reasons:
        candidate.reasons.append(reason)
    candidate.score = max(candidate.score, score)
    if line is not None and candidate.line is None:
        candidate.line = line
        candidate.snippet = compact_line(snippet)
        candidate.match = match


def iter_related_files(root: Path, protected_paths: list[str]) -> list[Path]:
    files: list[Path] = []
    root = root.resolve()
    for file_path in root.rglob("*"):
        if len(files) >= MAX_SCAN_FILES:
            break
        if not file_path.is_file():
            continue
        if not is_within_workspace(root, file_path):
            continue
        try:
            rel = file_path.relative_to(root)
        except ValueError:
            continue
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        rel_str = str(rel).replace("\\", "/")
        if is_protected_path(rel_str, protected_paths):
            continue
        if not is_related_extension(file_path):
            continue
        files.append(file_path)
    return files


def is_related_extension(path: Path) -> bool:
    if path.suffix.lower() in RELATED_EXTENSIONS:
        return True
    return any(suffix.lower() in RELATED_EXTENSIONS for suffix in path.suffixes)


def normalized_stem(path: Path) -> str:
    name = path.name.lower()
    for suffix in reversed(path.suffixes):
        if name.endswith(suffix.lower()):
            name = name[: -len(suffix)]
    for marker in ("test_", "spec_"):
        if name.startswith(marker):
            name = name[len(marker) :]
    for marker in ("_test", "_spec", "-test", "-spec", ".test", ".spec"):
        if name.endswith(marker):
            name = name[: -len(marker)]
    return name


def is_test_file(root: Path, path: Path) -> bool:
    rel = path.resolve().relative_to(root.resolve())
    lowered_parts = [part.lower() for part in rel.parts]
    if any(part in TEST_DIR_NAMES for part in lowered_parts[:-1]):
        return True
    name = path.name.lower()
    return any(marker in name for marker in TEST_MARKERS)


def reference_search_tokens(root: Path, target: Path) -> list[str]:
    rel = target.resolve().relative_to(root.resolve())
    base = normalized_stem(target)
    tokens = {base, target.name}
    raw_module_parts = rel.with_suffix("").parts
    if raw_module_parts:
        tokens.add(".".join(raw_module_parts))
    module_parts = [part for part in raw_module_parts if part not in {"src", "lib", "app"}]
    if module_parts:
        tokens.add(".".join(module_parts))
        tokens.add(module_parts[-1])
    return sorted(token for token in tokens if len(token) >= 3)


def first_reference(path: Path, tokens: list[str]) -> tuple[int, str, str] | None:
    try:
        if path.stat().st_size > MAX_REFERENCE_FILE_BYTES:
            return None
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return None

    patterns = [(token, re.compile(rf"(?<![\w.]){re.escape(token)}(?![\w.])")) for token in tokens]
    for line_number, line in enumerate(text.splitlines(), start=1):
        for token, pattern in patterns:
            if pattern.search(line):
                return line_number, line, token
    return None


def compact_line(line: str) -> str:
    line = " ".join(line.strip().split())
    if len(line) <= 120:
        return line
    return line[:117] + "..."
