from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.tools.base import ToolError, display_path, is_protected_path, resolve_workspace_path


class EditError(Exception):
    pass


class EditStaleError(EditError):
    pass


@dataclass(slots=True)
class EditProposal:
    path: str
    kind: str  # create | replace | delete
    diff: str
    new_content: str | None
    base_hash: str | None


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_edit_proposal(workspace: Path, arguments: dict, protected_paths: list[str]) -> EditProposal:
    raw_path = str(arguments.get("path") or "").strip()
    if not raw_path:
        raise EditError("path 不能为空")
    try:
        target = resolve_workspace_path(workspace, raw_path)
    except ToolError as exc:
        raise EditError(str(exc)) from exc
    rel = display_path(workspace, target)
    if is_protected_path(rel, protected_paths):
        raise EditError(f"受保护路径不可修改: {rel}")

    old_text = str(arguments.get("old_text") or "")
    new_text = str(arguments.get("new_text") or "")
    delete = bool(arguments.get("delete"))

    if delete:
        if not target.is_file():
            raise EditError(f"文件不存在，无法删除: {rel}")
        original = target.read_text("utf-8", errors="replace")
        diff = unified_diff(original, "", rel)
        return EditProposal(path=rel, kind="delete", diff=diff, new_content=None, base_hash=file_hash(target))

    if not target.exists():
        if old_text:
            raise EditError(f"文件不存在: {rel}")
        if not new_text:
            raise EditError("创建文件时 new_text 不能为空")
        diff = unified_diff("", new_text, rel)
        return EditProposal(path=rel, kind="create", diff=diff, new_content=new_text, base_hash=None)

    if not target.is_file():
        raise EditError(f"不是普通文件: {rel}")
    if not old_text:
        raise EditError("修改已有文件必须提供 old_text（文件中完整且唯一的原文片段）")
    original = target.read_text("utf-8", errors="replace")
    count = original.count(old_text)
    if count == 0:
        raise EditError(f"未找到 old_text，请先 read_file 确认原文: {rel}")
    if count > 1:
        raise EditError(f"old_text 在文件中出现 {count} 次，不唯一，请扩大片段范围: {rel}")
    updated = original.replace(old_text, new_text, 1)
    diff = unified_diff(original, updated, rel)
    return EditProposal(path=rel, kind="replace", diff=diff, new_content=updated, base_hash=file_hash(target))


def apply_edit(workspace: Path, proposal: EditProposal) -> None:
    target = resolve_workspace_path(workspace, proposal.path)
    if proposal.kind == "create":
        if target.exists():
            raise EditStaleError(f"文件已被外部创建: {proposal.path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(proposal.new_content or "", encoding="utf-8")
        return
    if not target.is_file():
        raise EditStaleError(f"文件已被外部删除: {proposal.path}")
    if proposal.base_hash and file_hash(target) != proposal.base_hash:
        raise EditStaleError(f"文件已被外部修改，请重新 read_file 后再试: {proposal.path}")
    if proposal.kind == "delete":
        target.unlink()
        return
    target.write_text(proposal.new_content or "", encoding="utf-8")


def unified_diff(original: str, updated: str, rel_path: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )
