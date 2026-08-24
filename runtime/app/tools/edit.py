from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.security import contains_known_environment_secret
from app.tools.base import ToolContext, ToolError, display_path, reject_protected_path, resolve_workspace_path


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


def require_prior_read(context: ToolContext, rel: str) -> None:
    """Refuse to edit a file this session has not read.

    `base_hash` already catches "read, then changed externally". It cannot catch
    "never read at all" — a model can invent `old_text`, and the resulting
    mismatch is indistinguishable to it from "the file simply differs". Requiring
    a prior read turns that guess into an explicit instruction to go look.

    Enforced only when a session is present. The Agent Loop always supplies one;
    an embedder driving the edit tools directly has no session-scoped state to
    check against.
    """
    session = getattr(context, "session", None)
    if session is None:
        return
    if session.read_hash(rel) is None:
        raise EditError(
            f"read_file has not been called for {rel} in this session; read it before editing "
            "so the edit is based on its actual current content"
        )


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_existing_text(target: Path, rel: str) -> str:
    try:
        return target.read_text("utf-8")
    except UnicodeDecodeError as exc:
        raise EditError(f"file is not editable UTF-8 text: {rel}") from exc


def build_edit_proposal(context: ToolContext, arguments: dict) -> EditProposal:
    """Turn edit arguments into a reviewable proposal.

    Takes the whole context rather than (workspace, protected_paths) because the
    read-before-write check needs the session's read record, and all three come
    from the same place.
    """
    workspace = context.workspace
    protected_paths = context.protected_paths
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

    delete = bool(arguments.get("delete"))
    edits = resolve_edits(arguments)
    new_text = edits[0][1] if edits else ""
    for old_fragment, new_fragment in edits:
        if contains_known_environment_secret(old_fragment) or contains_known_environment_secret(new_fragment):
            raise EditError("edit content contains a sensitive Runtime environment value; write denied")

    if delete:
        if not target.is_file():
            raise EditError(f"file does not exist and cannot be deleted: {rel}")
        require_prior_read(context, rel)
        original = _read_existing_text(target, rel)
        diff = unified_diff(original, "", rel)
        return EditProposal(path=rel, kind="delete", diff=diff, new_content=None, base_hash=file_hash(target))

    if target.exists():
        # `create` is exempt below: there is nothing to have read.
        require_prior_read(context, rel)

    if not target.exists():
        if len(edits) > 1:
            raise EditError(f"file does not exist, so there is nothing to replace: {rel}")
        if edits and edits[0][0]:
            raise EditError(f"file does not exist: {rel}")
        if not new_text:
            raise EditError("new_text must not be empty when creating a file")
        diff = unified_diff("", new_text, rel)
        return EditProposal(path=rel, kind="create", diff=diff, new_content=new_text, base_hash=None)

    if not target.is_file():
        raise EditError(f"path is not a regular file: {rel}")
    original = _read_existing_text(target, rel)
    updated = apply_replacements(original, edits, rel)
    diff = unified_diff(original, updated, rel)
    return EditProposal(path=rel, kind="replace", diff=diff, new_content=updated, base_hash=file_hash(target))


def resolve_edits(arguments: dict) -> list[tuple[str, str]]:
    """Normalise the single and batch forms into one list of replacements.

    The single `old_text`/`new_text` form is the length-1 case of `edits`, so the
    rest of the pipeline only ever deals with a list.
    """
    raw = arguments.get("edits")
    if raw is None:
        return [(str(arguments.get("old_text") or ""), str(arguments.get("new_text") or ""))]
    if not isinstance(raw, list) or not raw:
        raise EditError("edits must be a non-empty array")
    pairs: list[tuple[str, str]] = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise EditError(f"edit {index} must be an object with old_text and new_text")
        pairs.append((str(entry.get("old_text") or ""), str(entry.get("new_text") or "")))
    return pairs


def apply_replacements(original: str, edits: list[tuple[str, str]], rel: str) -> str:
    """Apply every replacement against the same original text.

    Deliberately not sequential `str.replace` calls on progressively-updated
    text: a later `old_text` would then be matched against content produced by an
    earlier replacement — text the model never saw — which can silently edit the
    wrong place.

    Every fragment is located in the original, the spans are checked for overlap,
    and the result is spliced in one pass. Any failure raises before anything is
    written, so an edit is never half-applied; a partially applied change is
    harder to recover from than a rejected one.
    """
    if not edits:
        raise EditError("editing an existing file requires at least one replacement")
    spans: list[tuple[int, int, str, int]] = []
    for index, (old_text, new_text) in enumerate(edits, start=1):
        label = f"edit {index}: " if len(edits) > 1 else ""
        if not old_text:
            raise EditError(f"{label}editing an existing file requires old_text containing an exact, unique source fragment")
        count = original.count(old_text)
        if count == 0:
            raise EditError(f"{label}old_text was not found; use read_file to confirm the source first: {rel}")
        if count > 1:
            raise EditError(f"{label}old_text occurs {count} times and is not unique; expand the source fragment: {rel}")
        start = original.index(old_text)
        spans.append((start, start + len(old_text), new_text, index))

    spans.sort()
    for (_, previous_end, _, previous_index), (start, _, _, index) in zip(spans, spans[1:], strict=False):
        if start < previous_end:
            raise EditError(
                f"edits {previous_index} and {index} overlap in {rel}; combine them into a single replacement"
            )

    pieces: list[str] = []
    cursor = 0
    for start, end, new_text, _ in spans:
        pieces.append(original[cursor:start])
        pieces.append(new_text)
        cursor = end
    pieces.append(original[cursor:])
    return "".join(pieces)


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
