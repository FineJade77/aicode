from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.security.secrets import contains_known_environment_secret
from app.tools.base import ToolError, display_path, reject_protected_path, resolve_workspace_path


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


def _read_existing_text(target: Path, rel: str) -> str:
    try:
        return target.read_text("utf-8")
    except UnicodeDecodeError as exc:
        raise EditError(f"file is not editable UTF-8 text: {rel}") from exc


def build_edit_proposal(workspace: Path, arguments: dict, protected_paths: list[str]) -> EditProposal:
    raw_path = str(arguments.get("path") or "").strip()
    if not raw_path:
        raise EditError("path must not be empty")
    try:
        target = resolve_workspace_path(workspace, raw_path)
    except ToolError as exc:
        raise EditError(str(exc)) from exc
    rel = display_path(workspace, target)
    try:
        reject_protected_path(workspace, target, protected_paths)
    except ToolError as exc:
        raise EditError(str(exc)) from exc

    old_text = str(arguments.get("old_text") or "")
    new_text = str(arguments.get("new_text") or "")
    delete = bool(arguments.get("delete"))
    if contains_known_environment_secret(old_text) or contains_known_environment_secret(new_text):
        raise EditError("edit content contains a sensitive Runtime environment value; write denied")

    if delete:
        if not target.is_file():
            raise EditError(f"file does not exist and cannot be deleted: {rel}")
        original = _read_existing_text(target, rel)
        diff = unified_diff(original, "", rel)
        return EditProposal(path=rel, kind="delete", diff=diff, new_content=None, base_hash=file_hash(target))

    if not target.exists():
        if old_text:
            raise EditError(f"file does not exist: {rel}")
        if not new_text:
            raise EditError("new_text must not be empty when creating a file")
        diff = unified_diff("", new_text, rel)
        return EditProposal(path=rel, kind="create", diff=diff, new_content=new_text, base_hash=None)

    if not target.is_file():
        raise EditError(f"path is not a regular file: {rel}")
    if not old_text:
        raise EditError("editing an existing file requires old_text containing an exact, unique source fragment")
    original = _read_existing_text(target, rel)
    count = original.count(old_text)
    if count == 0:
        raise EditError(f"old_text was not found; use read_file to confirm the source first: {rel}")
    if count > 1:
        raise EditError(f"old_text occurs {count} times and is not unique; expand the source fragment: {rel}")
    updated = original.replace(old_text, new_text, 1)
    diff = unified_diff(original, updated, rel)
    return EditProposal(path=rel, kind="replace", diff=diff, new_content=updated, base_hash=file_hash(target))


def apply_edit(workspace: Path, proposal: EditProposal) -> None:
    target = resolve_workspace_path(workspace, proposal.path)
    if proposal.kind == "create":
        if target.exists():
            raise EditStaleError(f"file was created externally: {proposal.path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(proposal.new_content or "", encoding="utf-8")
        return
    if not target.is_file():
        raise EditStaleError(f"file was deleted externally: {proposal.path}")
    if file_hash(target) != proposal.base_hash:
        raise EditStaleError(f"file was modified externally; use read_file again before retrying: {proposal.path}")
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
