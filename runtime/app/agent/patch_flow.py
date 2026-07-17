from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent.context_budget import budgeted_observations_for_model
from app.agent.steps import extract_json_object
from app.agent.tool_flow import execute_tool
from app.agent.types import AgentRequest, AgentRuntime
from app.agent.utils import localized, truncate_for_model
from app.audit.logger import stable_hash
from app.project.config import load_project_config
from app.project.detect import detect_test_command
from app.sessions.store import Session
from app.tools.base import ToolError, display_path, reject_protected_path, resolve_workspace_path
from app.tools.patch import (
    PatchApplication,
    PatchProposal,
    apply_content_patches,
    create_append_patch,
    create_content_patch,
    create_delete_patch,
    create_file_patch,
    create_rename_patch,
    create_replace_patch,
)


MAX_CODER_PATCH_OPERATIONS = 8
SUPPORTED_PATCH_SCHEMA_VERSION = 1
MAX_PATCH_DIFF_BYTES = 120_000


@dataclass(slots=True)
class CoderPatch:
    operation: str
    path: str
    new_path: str = ""
    old_text: str = ""
    new_text: str = ""
    content: str = ""
    text: str = ""
    reason: str = ""


@dataclass(slots=True)
class CoderPatchBundle:
    operations: list[CoderPatch]
    reason: str = ""


@dataclass(slots=True)
class PatchProposalEntry:
    operation: str
    proposal: PatchProposal


def build_coder_patch_messages(request: AgentRequest, observations: list[dict[str, Any]]) -> list[dict[str, str]]:
    language_name = "English" if request.language.startswith("en") else "中文"
    system = (
        f"你是 aicode 的 coder。使用{language_name}思考，但只能输出一个 JSON object。"
        "不要输出 Markdown，不要解释。"
        "你不能直接修改文件，只能提出一个结构化 patch proposal。"
        "只允许修改主 workspace 内的文件，不允许跨仓库写入。"
        "如果已有 patch 因 stale 或 patch 已过期失败，必须基于当前工具输出重新生成最小 patch。"
        "如果已有 patch 后验证失败，优先根据 verification.analysis、失败用例和相关工具输出提出最小修复。"
        "如果 observation 带 context_compacted，说明部分输出被预算层压缩；不要猜测被省略内容。"
        "如果上下文不足或不需要修改，输出 {\"action\":\"none\",\"reason\":\"...\"}。"
        "允许格式之一："
        "{\"action\":\"patch\",\"schema_version\":1,\"operations\":["
        "{\"operation\":\"replace\",\"path\":\"relative/path\",\"old_text\":\"exact existing text\",\"new_text\":\"replacement text\"},"
        "{\"operation\":\"append\",\"path\":\"relative/path\",\"text\":\"text to append\"},"
        "{\"operation\":\"create\",\"path\":\"relative/path\",\"content\":\"new file content\"},"
        "{\"operation\":\"delete\",\"path\":\"relative/path\"},"
        "{\"operation\":\"rename\",\"path\":\"old/path\",\"new_path\":\"new/path\"}"
        "],\"reason\":\"...\"}；"
        "{\"action\":\"patch\",\"operation\":\"replace\",\"path\":\"relative/path\",\"old_text\":\"exact existing text\",\"new_text\":\"replacement text\",\"reason\":\"...\"}；"
        "{\"action\":\"patch\",\"operation\":\"append\",\"path\":\"relative/path\",\"text\":\"text to append\",\"reason\":\"...\"}；"
        "{\"action\":\"patch\",\"operation\":\"create\",\"path\":\"relative/path\",\"content\":\"new file content\",\"reason\":\"...\"}。"
        "replace 的 old_text 必须是文件中完整且唯一存在的原文片段。"
        "多操作 proposal 最多包含 8 个 operations；同一文件可连续 replace/append/create/delete，但 rename 不能和同一路径的其它操作混用。"
        f"最终 diff 不能超过 {MAX_PATCH_DIFF_BYTES} bytes。"
    )
    budgeted_observations, context_budget = budgeted_observations_for_model(observations, "coder")
    payload = {
        "user_request": request.message,
        "mode": request.mode,
        "workspace": request.workspace,
        "observations": budgeted_observations,
        "context_budget": context_budget,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def parse_coder_patch(text: str) -> CoderPatch | CoderPatchBundle | None:
    raw = extract_json_object(text)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    action = str(payload.get("action") or "").strip().lower()
    if action in {"", "none", "finish", "no_patch"}:
        return None
    if action != "patch":
        return None
    if not supported_patch_schema_version(payload):
        return None

    reason = str(payload.get("reason") or "").strip()
    operations = payload.get("operations") or payload.get("patches")
    if isinstance(operations, list):
        return parse_coder_patch_bundle(operations, reason)

    return parse_coder_patch_operation(payload, reason)


def supported_patch_schema_version(payload: dict[str, Any]) -> bool:
    raw = payload.get("schema_version", payload.get("version", SUPPORTED_PATCH_SCHEMA_VERSION))
    try:
        return int(raw) == SUPPORTED_PATCH_SCHEMA_VERSION
    except (TypeError, ValueError):
        return False


def parse_coder_patch_bundle(operations: list[Any], reason: str) -> CoderPatchBundle | None:
    if not operations or len(operations) > MAX_CODER_PATCH_OPERATIONS:
        return None

    patches: list[CoderPatch] = []
    renamed_paths: set[str] = set()
    for item in operations:
        if not isinstance(item, dict):
            return None
        patch = parse_coder_patch_operation(item, str(item.get("reason") or reason).strip())
        if patch is None:
            return None
        normalized = normalize_coder_patch_path(patch.path)
        if patch.operation == "rename":
            if normalized in renamed_paths:
                return None
            renamed_paths.add(normalized)
        patches.append(patch)

    return CoderPatchBundle(operations=patches, reason=reason)


def parse_coder_patch_operation(payload: dict[str, Any], reason: str) -> CoderPatch | None:
    operation = str(payload.get("operation") or "").strip().lower()
    path = str(payload.get("path") or "").strip()
    if not valid_coder_patch_path(path):
        return None

    if operation == "replace":
        old_text = str(payload.get("old_text") or "")
        if "new_text" not in payload:
            return None
        new_text = str(payload.get("new_text") or "")
        if not old_text:
            return None
        return CoderPatch(operation=operation, path=path, old_text=old_text, new_text=new_text, reason=reason)

    if operation == "append":
        append_text = str(payload.get("text") or payload.get("content") or "")
        if not append_text:
            return None
        return CoderPatch(operation=operation, path=path, text=append_text, reason=reason)

    if operation == "create":
        content = str(payload.get("content") or "")
        if not content:
            return None
        return CoderPatch(operation=operation, path=path, content=content, reason=reason)

    if operation == "delete":
        return CoderPatch(operation=operation, path=path, reason=reason)

    if operation == "rename":
        new_path = str(payload.get("new_path") or payload.get("to") or "").strip()
        if not valid_coder_patch_path(new_path):
            return None
        if normalize_coder_patch_path(new_path) == normalize_coder_patch_path(path):
            return None
        return CoderPatch(operation=operation, path=path, new_path=new_path, reason=reason)

    return None


def valid_coder_patch_path(path: str) -> bool:
    if not path or path.startswith("/") or path.startswith("~"):
        return False
    normalized = path.replace("\\", "/")
    if normalized.startswith("../") or "/../" in normalized or normalized == "..":
        return False
    if ":" in normalized.split("/", 1)[0]:
        return False
    return True


def normalize_coder_patch_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


async def propose_coder_patch(session: Session, request: AgentRequest, patch: CoderPatch | CoderPatchBundle, runtime: AgentRuntime) -> dict[str, Any]:
    if isinstance(patch, CoderPatchBundle):
        return await propose_coder_patch_bundle(session, request, patch, runtime)
    if patch.operation == "replace":
        return await propose_replace_patch(session, request, patch.path, patch.old_text, patch.new_text, runtime)
    if patch.operation == "append":
        return await propose_append_patch(session, request, patch.path, patch.text, runtime)
    if patch.operation == "create":
        return await propose_create_patch(session, request, patch.path, patch.content, runtime)
    if patch.operation in {"delete", "rename"}:
        return await propose_coder_patch_bundle(session, request, CoderPatchBundle(operations=[patch], reason=patch.reason), runtime)
    await emit_patch_generation_error(session, f"unsupported coder patch operation: {patch.operation}", runtime)
    return patch_outcome("error", patch.operation, [patch.path], reason=f"unsupported coder patch operation: {patch.operation}")


async def propose_coder_patch_bundle(session: Session, request: AgentRequest, bundle: CoderPatchBundle, runtime: AgentRuntime) -> dict[str, Any]:
    if request.mode == "review":
        await emit_patch_write_denied(session, request)
        return patch_outcome("denied", "multi", [], reason=localized(request.language, "review 模式禁止写入", "review mode forbids writes"))

    try:
        project_config = load_project_config(Path(request.workspace))
        entries = create_proposals_for_coder_patches(Path(request.workspace), bundle.operations, project_config.protected_paths)
    except (ToolError, UnicodeDecodeError) as exc:
        await emit_patch_generation_error(session, str(exc), runtime)
        return patch_outcome("error", "multi", [], reason=str(exc))

    return await propose_patch_bundle(session, request, entries, runtime=runtime)


def create_proposal_for_coder_patch(workspace: Path, patch: CoderPatch, protected_paths: list[str]) -> PatchProposal:
    if patch.operation == "replace":
        return create_replace_patch(workspace, patch.path, patch.old_text, patch.new_text, protected_paths=protected_paths)
    if patch.operation == "append":
        return create_append_patch(workspace, patch.path, patch.text, protected_paths=protected_paths)
    if patch.operation == "create":
        return create_file_patch(workspace, patch.path, patch.content, protected_paths=protected_paths)
    if patch.operation == "delete":
        return create_delete_patch(workspace, patch.path, protected_paths=protected_paths)
    if patch.operation == "rename":
        return create_rename_patch(workspace, patch.path, patch.new_path, protected_paths=protected_paths)
    raise ToolError(f"unsupported coder patch operation: {patch.operation}")


@dataclass(slots=True)
class FileEditState:
    path: str
    original_exists: bool
    original_content: str
    current_content: str
    created: bool = False
    deleted: bool = False


def create_proposals_for_coder_patches(workspace: Path, patches: list[CoderPatch], protected_paths: list[str]) -> list[PatchProposalEntry]:
    states: dict[str, FileEditState] = {}
    entries: list[PatchProposalEntry] = []
    touched_by_rename: set[str] = set()

    for patch in patches:
        normalized = normalize_coder_patch_path(patch.path)
        if patch.operation == "rename":
            if normalized in states:
                raise ToolError(f"rename 不能和同一路径的其它操作混用: {patch.path}")
            touched_by_rename.add(normalized)
            entries.append(PatchProposalEntry(operation="rename", proposal=create_proposal_for_coder_patch(workspace, patch, protected_paths)))
            continue
        if normalized in touched_by_rename:
            raise ToolError(f"rename 不能和同一路径的其它操作混用: {patch.path}")

        state = states.get(normalized)
        if state is None:
            state = load_file_edit_state(workspace, patch.path, protected_paths)
            states[normalized] = state
        apply_coder_patch_to_state(state, patch)

    for state in states.values():
        if state.created and state.deleted:
            raise ToolError(f"不能在同一 proposal 中创建后删除文件: {state.path}")
        if state.deleted:
            entries.append(PatchProposalEntry(operation="delete", proposal=create_delete_patch(workspace, state.path, protected_paths=protected_paths)))
        else:
            entries.append(
                PatchProposalEntry(
                    operation="create" if state.created else "update",
                    proposal=create_content_patch(workspace, state.path, state.current_content, protected_paths=protected_paths, allow_create=state.created),
                )
            )
    return entries


def load_file_edit_state(workspace: Path, raw_path: str, protected_paths: list[str]) -> FileEditState:
    path = resolve_workspace_path(workspace, raw_path)
    reject_protected_path(workspace, path, protected_paths)
    if path.exists() and not path.is_file():
        raise ToolError(f"不是文件: {display_path(workspace, path)}")
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    return FileEditState(
        path=display_path(workspace, path),
        original_exists=path.exists(),
        original_content=original,
        current_content=original,
    )


def apply_coder_patch_to_state(state: FileEditState, patch: CoderPatch) -> None:
    if state.deleted:
        raise ToolError(f"文件已在 proposal 中删除: {state.path}")
    if patch.operation == "create":
        if state.original_exists or state.created:
            raise ToolError(f"文件已存在: {state.path}")
        state.current_content = ensure_trailing_newline(patch.content)
        state.created = True
        return
    if patch.operation == "replace":
        if not state.original_exists and not state.created:
            raise ToolError(f"文件不存在: {state.path}")
        occurrences = state.current_content.count(patch.old_text)
        if occurrences == 0:
            raise ToolError("替换前文本未在文件中找到")
        if occurrences > 1:
            raise ToolError(f"替换前文本出现 {occurrences} 次，请提供更精确的片段")
        state.current_content = state.current_content.replace(patch.old_text, patch.new_text, 1)
        return
    if patch.operation == "append":
        if not state.original_exists and not state.created:
            raise ToolError(f"文件不存在: {state.path}")
        append_text = ensure_trailing_newline(patch.text)
        separator = "" if state.current_content == "" or state.current_content.endswith("\n") else "\n"
        state.current_content = state.current_content + separator + append_text
        return
    if patch.operation == "delete":
        if not state.original_exists:
            raise ToolError(f"文件不存在: {state.path}")
        state.deleted = True
        return
    raise ToolError(f"unsupported coder patch operation: {patch.operation}")


def ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


async def propose_append_patch(session: Session, request: AgentRequest, path: str, text: str, runtime: AgentRuntime) -> dict[str, Any]:
    if request.mode == "review":
        await emit_patch_write_denied(session, request)
        return patch_outcome("denied", "append", [], reason=localized(request.language, "review 模式禁止写入", "review mode forbids writes"))

    try:
        project_config = load_project_config(Path(request.workspace))
        proposal = create_append_patch(Path(request.workspace), path, text, protected_paths=project_config.protected_paths)
    except (ToolError, UnicodeDecodeError) as exc:
        await emit_patch_generation_error(session, str(exc), runtime)
        return patch_outcome("error", "append", [], reason=str(exc))

    return await propose_patch(session, request, proposal, operation="append", runtime=runtime)


async def propose_replace_patch(
    session: Session, request: AgentRequest, path: str, old_text: str, new_text: str, runtime: AgentRuntime
) -> dict[str, Any]:
    if request.mode == "review":
        await emit_patch_write_denied(session, request)
        return patch_outcome("denied", "replace", [], reason=localized(request.language, "review 模式禁止写入", "review mode forbids writes"))

    try:
        project_config = load_project_config(Path(request.workspace))
        proposal = create_replace_patch(Path(request.workspace), path, old_text, new_text, protected_paths=project_config.protected_paths)
    except (ToolError, UnicodeDecodeError) as exc:
        await emit_patch_generation_error(session, str(exc), runtime)
        return patch_outcome("error", "replace", [], reason=str(exc))

    return await propose_patch(session, request, proposal, operation="replace", runtime=runtime)


async def propose_create_patch(session: Session, request: AgentRequest, path: str, content: str, runtime: AgentRuntime) -> dict[str, Any]:
    if request.mode == "review":
        await emit_patch_write_denied(session, request)
        return patch_outcome("denied", "create", [], reason=localized(request.language, "review 模式禁止写入", "review mode forbids writes"))

    try:
        project_config = load_project_config(Path(request.workspace))
        proposal = create_file_patch(Path(request.workspace), path, content, protected_paths=project_config.protected_paths)
    except (ToolError, UnicodeDecodeError) as exc:
        await emit_patch_generation_error(session, str(exc), runtime)
        return patch_outcome("error", "create", [], reason=str(exc))

    return await propose_patch(session, request, proposal, operation="create", runtime=runtime)


async def propose_patch(
    session: Session, request: AgentRequest, proposal: PatchProposal, operation: str, runtime: AgentRuntime
) -> dict[str, Any]:
    return await propose_patch_entries(
        session,
        request,
        [PatchProposalEntry(operation=operation, proposal=proposal)],
        operation=operation,
        runtime=runtime,
    )


async def propose_patch_bundle(
    session: Session, request: AgentRequest, entries: list[PatchProposalEntry], runtime: AgentRuntime
) -> dict[str, Any]:
    return await propose_patch_entries(session, request, entries, operation="multi", runtime=runtime)


async def propose_patch_entries(
    session: Session,
    request: AgentRequest,
    entries: list[PatchProposalEntry],
    *,
    operation: str,
    runtime: AgentRuntime,
) -> dict[str, Any]:
    if request.mode == "review":
        await emit_patch_write_denied(session, request)
        return patch_outcome("denied", operation, [], reason=localized(request.language, "review 模式禁止写入", "review mode forbids writes"))
    if not entries:
        await emit_patch_generation_error(session, "empty patch proposal", runtime)
        return patch_outcome("error", operation, [], reason="empty patch proposal")

    files = patch_entry_files(entries)
    diff = combine_patch_diffs([entry.proposal for entry in entries])
    diff_size = len(diff.encode("utf-8"))
    if diff_size > MAX_PATCH_DIFF_BYTES:
        reason = f"patch diff too large: {diff_size} bytes > {MAX_PATCH_DIFF_BYTES} bytes"
        await emit_patch_generation_error(session, reason, runtime)
        return patch_outcome("error", operation, files, reason=reason)
    approval_payload: dict[str, Any] = {
        "operation": operation,
        "files": files,
        "patches": [
            {
                "operation": entry.operation,
                "path": entry.proposal.path,
                "new_content": entry.proposal.new_content,
                "kind": entry.proposal.kind,
                "target_path": entry.proposal.target_path,
                "base_exists": entry.proposal.base_exists,
                "base_hash": entry.proposal.base_hash,
            }
            for entry in entries
        ],
    }
    if len(entries) == 1:
        approval_payload["path"] = entries[0].proposal.path
        approval_payload["new_content"] = entries[0].proposal.new_content
        approval_payload["kind"] = entries[0].proposal.kind
        approval_payload["target_path"] = entries[0].proposal.target_path
        approval_payload["base_exists"] = entries[0].proposal.base_exists
        approval_payload["base_hash"] = entries[0].proposal.base_hash
    approval = session.create_approval(
        "patch",
        approval_payload,
    )
    runtime.audit.record(
        "approval.requested",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "approval_id": approval.approval_id,
            "kind": "patch",
            "operation": operation,
            "files": files,
            "patch_hash": stable_hash(diff),
        },
    )
    await session.events.put(
        {
            "type": "approval.requested",
            "approval_id": approval.approval_id,
            "kind": "patch",
            "risk_level": "medium",
            "message": patch_approval_message(request.language, files),
        }
    )
    await session.events.put(
        {
            "type": "patch.preview",
            "approval_id": approval.approval_id,
            "files": files,
            "diff": diff,
        }
    )

    accepted = await session.wait_for_approval(approval.approval_id)
    if accepted is None:
        runtime.audit.record(
            "patch.rejected",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"approval_id": approval.approval_id, "reason": "timeout", "files": files},
        )
        await session.events.put(
            {
                "type": "patch.rejected",
                "approval_id": approval.approval_id,
                "reason": localized(request.language, "等待确认超时", "approval timed out"),
            }
        )
        return patch_outcome("timeout", operation, files, approval_id=approval.approval_id, reason="approval timed out")
    if not accepted:
        runtime.audit.record(
            "patch.rejected",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"approval_id": approval.approval_id, "reason": "user_rejected", "files": files},
        )
        await session.events.put(
            {
                "type": "patch.rejected",
                "approval_id": approval.approval_id,
                "reason": localized(request.language, "用户拒绝修改", "user rejected the patch"),
            }
        )
        return patch_outcome("rejected", operation, files, approval_id=approval.approval_id, reason="user rejected the patch")

    try:
        project_config = load_project_config(Path(request.workspace))
        apply_content_patches(
            Path(request.workspace),
            [
                PatchApplication(
                    path=entry.proposal.path,
                    new_content=entry.proposal.new_content,
                    allow_create=entry.proposal.kind == "write" and entry.operation == "create",
                    delete=entry.proposal.kind == "delete",
                    target_path=entry.proposal.target_path,
                    base_exists=entry.proposal.base_exists,
                    base_hash=entry.proposal.base_hash,
                )
                for entry in entries
            ],
            protected_paths=project_config.protected_paths,
        )
    except ToolError as exc:
        reason = str(exc)
        if is_stale_patch_error(reason):
            runtime.audit.record(
                "patch.stale",
                session_id=session.session_id,
                workspace=session.workspace,
                data={"approval_id": approval.approval_id, "files": files, "reason": reason},
            )
            await session.events.put(
                {
                    "type": "patch.stale",
                    "approval_id": approval.approval_id,
                    "files": files,
                    "reason": reason,
                    "message": localized(
                        request.language,
                        "Patch 已过期，文件在确认前发生变化；将尝试重新生成 diff。",
                        "Patch is stale because files changed before confirmation; attempting to rebuild the diff.",
                    ),
                }
            )
            return patch_outcome("stale", operation, files, approval_id=approval.approval_id, reason=reason)
        await session.events.put(
            {
                "type": "tool.error",
                "tool": "apply_patch",
                "error": reason,
                "risk_level": "medium",
                "requires_approval": True,
            }
        )
        return patch_outcome("error", operation, files, approval_id=approval.approval_id, reason=reason)

    runtime.audit.record(
        "patch.applied",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"approval_id": approval.approval_id, "files": files, "patch_hash": stable_hash(diff)},
    )
    await session.events.put(
        {
            "type": "patch.applied",
            "approval_id": approval.approval_id,
            "files": files,
        }
    )
    verification = await run_post_patch_verification(session, request, runtime)
    return patch_outcome("applied", operation, files, approval_id=approval.approval_id, verification=verification)


def patch_approval_message(language: str, files: list[str]) -> str:
    if len(files) == 1:
        return localized(language, f"是否允许修改 {files[0]}？", f"Allow changes to {files[0]}?")
    return localized(language, f"是否允许修改 {len(files)} 个文件？", f"Allow changes to {len(files)} files?")


def patch_entry_files(entries: list[PatchProposalEntry]) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        for path in [entry.proposal.path, entry.proposal.target_path]:
            if not path or path in seen:
                continue
            files.append(path)
            seen.add(path)
    return files


def combine_patch_diffs(proposals: list[PatchProposal]) -> str:
    parts = []
    for proposal in proposals:
        diff = proposal.diff.rstrip("\n")
        if diff:
            parts.append(diff)
    return "\n".join(parts) + ("\n" if parts else "")


async def run_post_patch_verification(session: Session, request: AgentRequest, runtime: AgentRuntime) -> dict[str, Any]:
    command = detect_test_command(Path(request.workspace))
    if command is None:
        await session.events.put(
            {
                "type": "verification.skipped",
                "reason": localized(request.language, "未发现可自动运行的测试命令", "no test command detected"),
            }
        )
        return {"status": "skipped", "reason": "no test command detected"}

    decision = runtime.tools.evaluate("run_shell", {"command": command}, mode=request.mode)
    if not decision.allowed or decision.requires_approval or decision.risk_level != "low":
        reason = verification_denied_reason(request.language, decision)
        runtime.audit.record(
            "verification.denied",
            session_id=session.session_id,
            workspace=session.workspace,
            data={
                "command": command,
                "risk_level": decision.risk_level,
                "requires_approval": decision.requires_approval,
                "policy_reason": decision.reason,
            },
        )
        await session.events.put(
            {
                "type": "verification.denied",
                "command": command,
                "risk_level": decision.risk_level,
                "requires_approval": decision.requires_approval,
                "reason": reason,
            }
        )
        return {
            "status": "denied",
            "command": command,
            "risk_level": decision.risk_level,
            "requires_approval": decision.requires_approval,
            "reason": reason,
        }

    await session.events.put(
        {
            "type": "verification.started",
            "command": command,
            "message": localized(request.language, f"尝试运行验证命令: {command}", f"Running verification command: {command}"),
        }
    )
    result = await execute_tool(session, request, "run_tests", {"timeout": 120}, runtime)
    analysis = result.data.get("analysis") if isinstance(result.data.get("analysis"), dict) else {}
    if not result.success and analysis:
        runtime.audit.record(
            "verification.analysis",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"command": command, "analysis": analysis},
        )
        await session.events.put(
            {
                "type": "verification.analysis",
                "command": command,
                "analysis": analysis,
            }
        )
    runtime.audit.record(
        "verification.completed",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "command": command,
            "success": result.success,
            "risk_level": result.risk_level,
            "requires_approval": result.requires_approval,
            "analysis": analysis,
        },
    )
    await session.events.put(
        {
            "type": "verification.completed",
            "command": command,
            "success": result.success,
            "risk_level": result.risk_level,
        }
    )
    return {
        "status": "passed" if result.success else "failed",
        "command": command,
        "risk_level": result.risk_level,
        "requires_approval": result.requires_approval,
        "analysis": analysis,
        "text": truncate_for_model(result.text or result.error, limit=4_000),
    }


def verification_denied_reason(language: str, decision: Any) -> str:
    if language.startswith("en"):
        if decision.requires_approval:
            return "verification command requires explicit approval and was not auto-run"
        if decision.risk_level == "high":
            return "verification command is blocked by policy"
        return "verification command was denied by policy"
    return decision.reason or "验证命令未通过安全策略"


def is_stale_patch_error(reason: str) -> bool:
    return reason.startswith("patch 已过期:")


def patch_outcome(
    status: str,
    operation: str,
    files: list[str],
    *,
    approval_id: str = "",
    reason: str = "",
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "tool": "apply_patch",
        "success": status == "applied",
        "status": status,
        "operation": operation,
        "files": files,
        "approval_id": approval_id,
        "reason": reason,
        "verification": verification or {},
    }


async def emit_patch_write_denied(session: Session, request: AgentRequest) -> None:
    await session.events.put(
        {
            "type": "tool.denied",
            "tool": "apply_patch",
            "error": localized(request.language, "review 模式禁止写入", "review mode forbids writes"),
            "risk_level": "high",
            "requires_approval": False,
        }
    )


async def emit_patch_generation_error(session: Session, error: str, runtime: AgentRuntime) -> None:
    runtime.audit.record(
        "tool.error",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"tool": "generate_patch", "error": error, "risk_level": "medium", "requires_approval": True},
    )
    await session.events.put(
        {
            "type": "tool.error",
            "tool": "generate_patch",
            "error": error,
            "risk_level": "medium",
            "requires_approval": True,
        }
    )
