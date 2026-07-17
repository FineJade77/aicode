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
    new_content: str = ""
    kind: str = "write"
    target_path: str = ""


@dataclass(slots=True)
class PatchApplication:
    path: str
    new_content: str = ""
    allow_create: bool = False
    delete: bool = False
    target_path: str = ""


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


def create_content_patch(
    workspace: Path,
    raw_path: str,
    new_content: str,
    protected_paths: list[str] | None = None,
    allow_create: bool = False,
) -> PatchProposal:
    protected_paths = protected_paths or default_protected_paths()
    path = resolve_workspace_path(workspace, raw_path)
    reject_protected_path(workspace, path, protected_paths)

    if path.exists() and not path.is_file():
        raise ToolError(f"不是文件: {display_path(workspace, path)}")
    if not path.exists() and not allow_create:
        raise ToolError(f"文件不存在: {display_path(workspace, path)}")
    if not path.parent.exists() or not path.parent.is_dir():
        raise ToolError(f"父目录不存在: {display_path(workspace, path.parent)}")

    original = path.read_text(encoding="utf-8") if path.exists() else ""
    rel = display_path(workspace, path)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{rel}" if path.exists() else "/dev/null",
            tofile=f"b/{rel}",
        )
    )
    return PatchProposal(path=rel, diff=diff, new_content=new_content)


def create_delete_patch(
    workspace: Path,
    raw_path: str,
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
    rel = display_path(workspace, path)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            [],
            fromfile=f"a/{rel}",
            tofile="/dev/null",
        )
    )
    return PatchProposal(path=rel, diff=diff, kind="delete")


def create_rename_patch(
    workspace: Path,
    raw_path: str,
    raw_new_path: str,
    protected_paths: list[str] | None = None,
) -> PatchProposal:
    protected_paths = protected_paths or default_protected_paths()
    path = resolve_workspace_path(workspace, raw_path)
    new_path = resolve_workspace_path(workspace, raw_new_path)
    reject_protected_path(workspace, path, protected_paths)
    reject_protected_path(workspace, new_path, protected_paths)

    if not path.exists():
        raise ToolError(f"文件不存在: {display_path(workspace, path)}")
    if not path.is_file():
        raise ToolError(f"不是文件: {display_path(workspace, path)}")
    if new_path.exists():
        raise ToolError(f"文件已存在: {display_path(workspace, new_path)}")
    if not new_path.parent.exists() or not new_path.parent.is_dir():
        raise ToolError(f"父目录不存在: {display_path(workspace, new_path.parent)}")

    rel = display_path(workspace, path)
    new_rel = display_path(workspace, new_path)
    diff = f"diff --git a/{rel} b/{new_rel}\nsimilarity index 100%\nrename from {rel}\nrename to {new_rel}\n"
    return PatchProposal(path=rel, diff=diff, kind="rename", target_path=new_rel)


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
    resolved: list[tuple[Path, Path | None, PatchApplication]] = []
    original_contents: dict[Path, str | None] = {}

    for patch in patches:
        path = resolve_workspace_path(workspace, patch.path)
        reject_protected_path(workspace, path, protected_paths)
        target_path = resolve_workspace_path(workspace, patch.target_path) if patch.target_path else None
        if target_path is not None:
            reject_protected_path(workspace, target_path, protected_paths)
        touched_paths = [path]
        if target_path is not None:
            touched_paths.append(target_path)
        if any(touched in original_contents for touched in touched_paths):
            raise ToolError(f"重复修改同一文件: {display_path(workspace, path)}")
        validate_patch_application(workspace, path, target_path, patch)
        for touched in touched_paths:
            original_contents[touched] = touched.read_text(encoding="utf-8") if touched.exists() else None
        resolved.append((path, target_path, patch))

    try:
        for path, target_path, patch in resolved:
            if patch.delete:
                path.unlink()
            elif target_path is not None:
                path.rename(target_path)
            else:
                path.write_text(patch.new_content, encoding="utf-8")
    except Exception as exc:
        rollback_content(workspace, original_contents)
        if isinstance(exc, ToolError):
            raise
        raise ToolError(str(exc)) from exc


def validate_patch_application(workspace: Path, path: Path, target_path: Path | None, patch: PatchApplication) -> None:
    if path.exists() and not path.is_file():
        raise ToolError(f"不是文件: {display_path(workspace, path)}")
    if patch.delete:
        if not path.exists():
            raise ToolError(f"文件不存在: {display_path(workspace, path)}")
        return
    if target_path is not None:
        if not path.exists():
            raise ToolError(f"文件不存在: {display_path(workspace, path)}")
        if target_path.exists():
            raise ToolError(f"文件已存在: {display_path(workspace, target_path)}")
        if not target_path.parent.exists() or not target_path.parent.is_dir():
            raise ToolError(f"父目录不存在: {display_path(workspace, target_path.parent)}")
        return
    if not path.exists() and not patch.allow_create:
        raise ToolError(f"文件不存在: {display_path(workspace, path)}")
    if not path.parent.exists() or not path.parent.is_dir():
        raise ToolError(f"父目录不存在: {display_path(workspace, path.parent)}")


def rollback_content(workspace: Path, original_contents: dict[Path, str | None]) -> None:
    for rollback_path, original in reversed(list(original_contents.items())):
        if original is None:
            if rollback_path.exists():
                rollback_path.unlink(missing_ok=True)
        else:
            if not rollback_path.parent.exists() or not rollback_path.parent.is_dir():
                raise ToolError(f"父目录不存在: {display_path(workspace, rollback_path.parent)}")
            rollback_path.write_text(original, encoding="utf-8")
