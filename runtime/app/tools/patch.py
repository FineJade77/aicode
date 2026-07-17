from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from app.project.config import default_protected_paths
from app.tools.base import ToolError, display_path, reject_protected_path, resolve_workspace_path


@dataclass(slots=True)
class PatchProposal:
    path: str
    diff: str
    new_content: str


@dataclass(slots=True)
class PatchApplication:
    path: str
    new_content: str
    allow_create: bool = False


def create_append_patch(
    workspace: Path,
    raw_path: str,
    append_text: str,
    protected_paths: list[str] | None = None,
) -> PatchProposal:
    protected_paths = protected_paths or default_protected_paths()
    path = resolve_workspace_path(workspace, raw_path)
    reject_protected_path(workspace, path, protected_paths)

    if not path.exists():
        raise ToolError(f"文件不存在: {display_path(workspace, path)}")
    if not path.is_file():
        raise ToolError(f"不是文件: {display_path(workspace, path)}")

    original = path.read_text(encoding="utf-8")
    text_to_append = append_text if append_text.endswith("\n") else append_text + "\n"
    separator = "" if original == "" or original.endswith("\n") else "\n"
    new_content = original + separator + text_to_append
    rel = display_path(workspace, path)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )
    return PatchProposal(path=rel, diff=diff, new_content=new_content)


def create_replace_patch(
    workspace: Path,
    raw_path: str,
    old_text: str,
    new_text: str,
    protected_paths: list[str] | None = None,
) -> PatchProposal:
    protected_paths = protected_paths or default_protected_paths()
    path = resolve_workspace_path(workspace, raw_path)
    reject_protected_path(workspace, path, protected_paths)

    if not path.exists():
        raise ToolError(f"文件不存在: {display_path(workspace, path)}")
    if not path.is_file():
        raise ToolError(f"不是文件: {display_path(workspace, path)}")
    if old_text == "":
        raise ToolError("替换前文本不能为空")

    original = path.read_text(encoding="utf-8")
    occurrences = original.count(old_text)
    if occurrences == 0:
        raise ToolError("替换前文本未在文件中找到")
    if occurrences > 1:
        raise ToolError(f"替换前文本出现 {occurrences} 次，请提供更精确的片段")

    new_content = original.replace(old_text, new_text, 1)
    rel = display_path(workspace, path)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )
    return PatchProposal(path=rel, diff=diff, new_content=new_content)


def create_file_patch(
    workspace: Path,
    raw_path: str,
    content: str,
    protected_paths: list[str] | None = None,
) -> PatchProposal:
    protected_paths = protected_paths or default_protected_paths()
    path = resolve_workspace_path(workspace, raw_path)
    reject_protected_path(workspace, path, protected_paths)

    if path.exists():
        raise ToolError(f"文件已存在: {display_path(workspace, path)}")
    if not path.parent.exists() or not path.parent.is_dir():
        raise ToolError(f"父目录不存在: {display_path(workspace, path.parent)}")

    new_content = content if content.endswith("\n") else content + "\n"
    rel = display_path(workspace, path)
    diff = "".join(
        difflib.unified_diff(
            [],
            new_content.splitlines(keepends=True),
            fromfile="/dev/null",
            tofile=f"b/{rel}",
        )
    )
    return PatchProposal(path=rel, diff=diff, new_content=new_content)


def apply_content_patch(
    workspace: Path,
    raw_path: str,
    new_content: str,
    protected_paths: list[str] | None = None,
    allow_create: bool = False,
) -> None:
    protected_paths = protected_paths or default_protected_paths()
    path = resolve_workspace_path(workspace, raw_path)
    reject_protected_path(workspace, path, protected_paths)
    if path.exists() and not path.is_file():
        raise ToolError(f"不是文件: {display_path(workspace, path)}")
    if not path.exists() and not allow_create:
        raise ToolError(f"文件不存在: {display_path(workspace, path)}")
    if not path.parent.exists() or not path.parent.is_dir():
        raise ToolError(f"父目录不存在: {display_path(workspace, path.parent)}")
    path.write_text(new_content, encoding="utf-8")


def apply_content_patches(
    workspace: Path,
    patches: list[PatchApplication],
    protected_paths: list[str] | None = None,
) -> None:
    protected_paths = protected_paths or default_protected_paths()
    resolved: list[tuple[Path, PatchApplication]] = []
    original_contents: dict[Path, str | None] = {}

    for patch in patches:
        path = resolve_workspace_path(workspace, patch.path)
        reject_protected_path(workspace, path, protected_paths)
        if path in original_contents:
            raise ToolError(f"重复修改同一文件: {display_path(workspace, path)}")
        if path.exists() and not path.is_file():
            raise ToolError(f"不是文件: {display_path(workspace, path)}")
        if not path.exists() and not patch.allow_create:
            raise ToolError(f"文件不存在: {display_path(workspace, path)}")
        if not path.parent.exists() or not path.parent.is_dir():
            raise ToolError(f"父目录不存在: {display_path(workspace, path.parent)}")
        original_contents[path] = path.read_text(encoding="utf-8") if path.exists() else None
        resolved.append((path, patch))

    written: list[Path] = []
    try:
        for path, patch in resolved:
            path.write_text(patch.new_content, encoding="utf-8")
            written.append(path)
    except Exception as exc:
        rollback_paths = written if path in written else [*written, path]
        for rollback_path in reversed(rollback_paths):
            original = original_contents.get(rollback_path)
            if original is None:
                rollback_path.unlink(missing_ok=True)
            else:
                rollback_path.write_text(original, encoding="utf-8")
        if isinstance(exc, ToolError):
            raise
        raise ToolError(str(exc)) from exc
