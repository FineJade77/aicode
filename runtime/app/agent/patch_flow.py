from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent.steps import extract_json_object
from app.agent.tool_flow import execute_tool
from app.agent.types import AgentRequest, AgentRuntime
from app.agent.utils import localized, truncate_for_model
from app.audit.logger import stable_hash
from app.project.config import load_project_config
from app.project.detect import detect_test_command
from app.sessions.store import Session
from app.tools.base import ToolError
from app.tools.patch import PatchProposal, apply_content_patch, create_append_patch, create_file_patch, create_replace_patch


@dataclass(slots=True)
class CoderPatch:
    operation: str
    path: str
    old_text: str = ""
    new_text: str = ""
    content: str = ""
    text: str = ""
    reason: str = ""


def build_coder_patch_messages(request: AgentRequest, observations: list[dict[str, Any]]) -> list[dict[str, str]]:
    language_name = "English" if request.language.startswith("en") else "中文"
    system = (
        f"你是 aicode 的 coder。使用{language_name}思考，但只能输出一个 JSON object。"
        "不要输出 Markdown，不要解释。"
        "你不能直接修改文件，只能提出一个结构化 patch proposal。"
        "只允许修改主 workspace 内的文件，不允许跨仓库写入。"
        "如果已有 patch 后验证失败，优先根据 verification.analysis、失败用例和相关工具输出提出最小修复。"
        "如果上下文不足或不需要修改，输出 {\"action\":\"none\",\"reason\":\"...\"}。"
        "允许格式之一："
        "{\"action\":\"patch\",\"operation\":\"replace\",\"path\":\"relative/path\",\"old_text\":\"exact existing text\",\"new_text\":\"replacement text\",\"reason\":\"...\"}；"
        "{\"action\":\"patch\",\"operation\":\"append\",\"path\":\"relative/path\",\"text\":\"text to append\",\"reason\":\"...\"}；"
        "{\"action\":\"patch\",\"operation\":\"create\",\"path\":\"relative/path\",\"content\":\"new file content\",\"reason\":\"...\"}。"
        "replace 的 old_text 必须是文件中完整且唯一存在的原文片段。"
    )
    payload = {
        "user_request": request.message,
        "mode": request.mode,
        "workspace": request.workspace,
        "observations": observations,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def parse_coder_patch(text: str) -> CoderPatch | None:
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

    operation = str(payload.get("operation") or "").strip().lower()
    path = str(payload.get("path") or "").strip()
    reason = str(payload.get("reason") or "").strip()
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


async def propose_coder_patch(session: Session, request: AgentRequest, patch: CoderPatch, runtime: AgentRuntime) -> dict[str, Any]:
    if patch.operation == "replace":
        return await propose_replace_patch(session, request, patch.path, patch.old_text, patch.new_text, runtime)
    if patch.operation == "append":
        return await propose_append_patch(session, request, patch.path, patch.text, runtime)
    if patch.operation == "create":
        return await propose_create_patch(session, request, patch.path, patch.content, runtime)
    await emit_patch_generation_error(session, f"unsupported coder patch operation: {patch.operation}", runtime)
    return patch_outcome("error", patch.operation, [patch.path], reason=f"unsupported coder patch operation: {patch.operation}")


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
    if request.mode == "review":
        await emit_patch_write_denied(session, request)
        return patch_outcome("denied", operation, [proposal.path], reason=localized(request.language, "review 模式禁止写入", "review mode forbids writes"))

    approval = session.create_approval(
        "patch",
        {
            "operation": operation,
            "path": proposal.path,
            "new_content": proposal.new_content,
        },
    )
    runtime.audit.record(
        "approval.requested",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "approval_id": approval.approval_id,
            "kind": "patch",
            "operation": operation,
            "files": [proposal.path],
            "patch_hash": stable_hash(proposal.diff),
        },
    )
    await session.events.put(
        {
            "type": "approval.requested",
            "approval_id": approval.approval_id,
            "kind": "patch",
            "risk_level": "medium",
            "message": localized(
                request.language,
                f"是否允许修改 {proposal.path}？",
                f"Allow changes to {proposal.path}?",
            ),
        }
    )
    await session.events.put(
        {
            "type": "patch.preview",
            "approval_id": approval.approval_id,
            "files": [proposal.path],
            "diff": proposal.diff,
        }
    )

    accepted = await session.wait_for_approval(approval.approval_id)
    if accepted is None:
        runtime.audit.record(
            "patch.rejected",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"approval_id": approval.approval_id, "reason": "timeout", "files": [proposal.path]},
        )
        await session.events.put(
            {
                "type": "patch.rejected",
                "approval_id": approval.approval_id,
                "reason": localized(request.language, "等待确认超时", "approval timed out"),
            }
        )
        return patch_outcome("timeout", operation, [proposal.path], approval_id=approval.approval_id, reason="approval timed out")
    if not accepted:
        runtime.audit.record(
            "patch.rejected",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"approval_id": approval.approval_id, "reason": "user_rejected", "files": [proposal.path]},
        )
        await session.events.put(
            {
                "type": "patch.rejected",
                "approval_id": approval.approval_id,
                "reason": localized(request.language, "用户拒绝修改", "user rejected the patch"),
            }
        )
        return patch_outcome("rejected", operation, [proposal.path], approval_id=approval.approval_id, reason="user rejected the patch")

    try:
        project_config = load_project_config(Path(request.workspace))
        apply_content_patch(
            Path(request.workspace),
            proposal.path,
            proposal.new_content,
            protected_paths=project_config.protected_paths,
            allow_create=operation == "create",
        )
    except ToolError as exc:
        await session.events.put(
            {
                "type": "tool.error",
                "tool": "apply_patch",
                "error": str(exc),
                "risk_level": "medium",
                "requires_approval": True,
            }
        )
        return patch_outcome("error", operation, [proposal.path], approval_id=approval.approval_id, reason=str(exc))

    runtime.audit.record(
        "patch.applied",
        session_id=session.session_id,
        workspace=session.workspace,
        data={"approval_id": approval.approval_id, "files": [proposal.path], "patch_hash": stable_hash(proposal.diff)},
    )
    await session.events.put(
        {
            "type": "patch.applied",
            "approval_id": approval.approval_id,
            "files": [proposal.path],
        }
    )
    verification = await run_post_patch_verification(session, request, runtime)
    return patch_outcome("applied", operation, [proposal.path], approval_id=approval.approval_id, verification=verification)


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
