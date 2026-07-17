from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent.tool_flow import execute_tool
from app.agent.types import AgentRequest, AgentRuntime
from app.agent.utils import localized, truncate_for_model
from app.audit.logger import stable_hash
from app.project.config import load_project_config
from app.project.detect import detect_test_command
from app.sessions.store import Session
from app.tools.base import ToolError
from app.tools.patch import PatchProposal, apply_content_patch, create_append_patch, create_file_patch, create_replace_patch


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
    runtime.audit.record(
        "verification.completed",
        session_id=session.session_id,
        workspace=session.workspace,
        data={
            "command": command,
            "success": result.success,
            "risk_level": result.risk_level,
            "requires_approval": result.requires_approval,
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
