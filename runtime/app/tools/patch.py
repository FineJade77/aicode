from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from app.tools.base import ToolError, display_path, reject_protected_path, resolve_workspace_path


@dataclass(slots=True)
class PatchProposal:
    path: str
    diff: str
    new_content: str


def create_append_patch(workspace: Path, raw_path: str, append_text: str) -> PatchProposal:
    path = resolve_workspace_path(workspace, raw_path)
    reject_protected_path(path)

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


def apply_content_patch(workspace: Path, raw_path: str, new_content: str) -> None:
    path = resolve_workspace_path(workspace, raw_path)
    reject_protected_path(path)
    if not path.exists() or not path.is_file():
        raise ToolError(f"文件不存在: {display_path(workspace, path)}")
    path.write_text(new_content, encoding="utf-8")
