from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any

from app.agent.loop import AgentLoop
from app.application.errors import Conflict, InvalidRequest, NotFound, ProviderUnavailable
from app.core.hashing import stable_hash
from app.core.ports import (
    Clock,
    ExecutionRuntime,
    ModelRuntime,
    ProjectTrustRepository,
    SessionRepository,
    TraceSink,
    UsageRuntime,
    WorkspaceRuntime,
)
from app.core.session import AgentSession
from app.execution import ExecutionRequest, ResourceLimits


class SessionService:
    def __init__(
        self,
        sessions: SessionRepository,
        trace: TraceSink,
        workspace: WorkspaceRuntime,
    ) -> None:
        self.sessions = sessions
        self.trace = trace
        self.workspace = workspace

    async def create(self, workspace: str, language: str) -> AgentSession:
        effective = self.workspace.effective_language(workspace, language)
        session = self.sessions.create(workspace=workspace, language=effective)
        self.trace.record(
            "session.created",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"language": session.language},
        )
        await session.events.put(
            {
                "type": "session.created",
                "session_id": session.session_id,
                "workspace": session.workspace,
            }
        )
        return session

    def get(self, session_id: str) -> AgentSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise NotFound("session not found")
        return session

    def list(self, *, last: bool = False) -> Any:
        if not last:
            return self.sessions.list()
        session = self.sessions.last()
        return None if session is None else session.to_dict()

    def bind_request(self, session: AgentSession, request: Any) -> Any:
        if not self.workspace.same_workspace(session.workspace, request.workspace):
            raise InvalidRequest("message workspace does not match session workspace")
        updates = {"workspace": session.workspace, "language": session.language}
        model_copy = getattr(request, "model_copy", None)
        if callable(model_copy):
            return model_copy(update=updates)
        if is_dataclass(request):
            return replace(request, **updates)
        raise InvalidRequest("unsupported message request type")


class RunCoordinator:
    def __init__(
        self,
        model: ModelRuntime,
        trace: TraceSink,
        agent_loop: AgentLoop,
    ) -> None:
        self.model = model
        self.trace = trace
        self.agent_loop = agent_loop

    async def submit(self, session: AgentSession, request: Any) -> dict[str, str]:
        configured = getattr(self.model.primary, "is_configured", None)
        if callable(configured) and not configured():
            raise ProviderUnavailable("模型 provider 未配置，请设置 API key（如 OPENAI_API_KEY 或 ANTHROPIC_API_KEY）后重试")
        was_running = session.agent_runner_active() or session.agent_queue.qsize() > 0
        session.events.set_default_after(session.events.last_event_id())
        queued = session.enqueue_agent_run(request)
        session.events.set_default_run_id(queued.run_id)
        queue_position = session.agent_queue.qsize()
        self.trace.record(
            "message.received",
            session_id=session.session_id,
            workspace=session.workspace,
            data={
                "mode": request.mode,
                "language": request.language,
                "message_hash": stable_hash(request.message),
                "message_preview": request.message[:200],
                "run_id": queued.run_id,
                "queued": was_running,
                "queue_position": queue_position,
            },
        )
        await self._emit_queued(session, queued, was_running, queue_position)
        self.ensure_runner(session)
        return {"status": "queued" if was_running else "accepted", "run_id": queued.run_id}

    def ensure_runner(self, session: AgentSession) -> None:
        if session.agent_runner_active():
            return
        session.agent_runner_task = asyncio.create_task(self._process_runs(session))

    async def cancel(self, session: AgentSession, *, resume_queued: bool = True) -> dict[str, Any]:
        task = session.agent_runner_task
        run_id = session.current_run_id
        if task is None or task.done() or run_id is None:
            return {"status": "idle", "run_id": None, "queued": session.agent_queue.qsize()}

        session.mark_agent_progress("cancelling")
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        session.agent_runner_task = None

        for approval in session.expire_pending_approvals():
            await session.events.put(
                {
                    "type": "approval.expired",
                    "run_id": run_id,
                    "approval_id": approval.approval_id,
                    "kind": approval.kind,
                    "message": "当前任务已取消，待确认操作已过期。",
                }
            )

        message = "当前任务已取消。"
        self.trace.record(
            "run.cancelled",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"run_id": run_id, "queued": session.agent_queue.qsize()},
        )
        await session.events.put({"type": "run.cancelled", "run_id": run_id, "status": "cancelled", "message": message})
        await session.events.put({"type": "final", "run_id": run_id, "status": "cancelled", "summary": message})
        queued = session.agent_queue.qsize()
        if queued and resume_queued:
            self.ensure_runner(session)
        return {"status": "cancelled", "run_id": run_id, "queued": queued}

    async def _process_runs(self, session: AgentSession) -> None:
        while True:
            queued = session.next_agent_run()
            if queued is None:
                return
            session.start_agent_run(queued.run_id)
            session.events.set_current_run_id(queued.run_id)
            try:
                session.mark_agent_progress("agent.loop")
                self.trace.record(
                    "run.started",
                    session_id=session.session_id,
                    workspace=session.workspace,
                    data={"run_id": queued.run_id},
                )
                await session.events.put({"type": "run.started", "message": "开始执行当前任务。"})
                await self.agent_loop.run(session, queued.request)
            finally:
                session.events.set_current_run_id(None)
                session.finish_agent_run()

    @staticmethod
    async def _emit_queued(session: AgentSession, queued: Any, was_running: bool, queue_position: int) -> None:
        await session.events.put(
            {
                "type": "run.queued",
                "run_id": queued.run_id,
                "status": "queued" if was_running else "accepted",
                "queue_position": queue_position,
                "message": (
                    "任务已排队，等待当前会话中的上一条任务完成。"
                    if was_running
                    else "任务已接收，准备开始执行。"
                ),
            }
        )


class ApprovalService:
    def __init__(self, trace: TraceSink) -> None:
        self.trace = trace

    def resolve(
        self,
        session: AgentSession,
        approval_id: str,
        *,
        accepted: bool,
        accept_all: bool = False,
    ) -> dict[str, str]:
        if accept_all and accepted:
            session.auto_accept_edits = True
            self.trace.record(
                "approval.accept_all_enabled",
                session_id=session.session_id,
                workspace=session.workspace,
                data={"approval_id": approval_id},
            )
        if not session.resolve_approval(approval_id, accepted=accepted):
            raise NotFound("approval not found or already resolved")
        self.trace.record(
            "approval.resolved",
            session_id=session.session_id,
            workspace=session.workspace,
            data={"approval_id": approval_id, "accepted": accepted},
        )
        return {"status": "accepted" if accepted else "rejected", "approval_id": approval_id}


class TraceService:
    def __init__(self, trace: TraceSink, usage: UsageRuntime, clock: Clock) -> None:
        self.trace = trace
        self.usage = usage
        self.clock = clock

    async def summarize(self, *, today: bool = False, session_id: str | None = None) -> dict[str, Any]:
        await self.trace.flush()
        return self.usage.summarize(session_id=session_id, day=self.clock.today() if today else None)


class ProjectTrustService:
    def __init__(self, trust: ProjectTrustRepository, trace: TraceSink) -> None:
        self.trust = trust
        self.trace = trace

    def get(self, workspace: str | None = None) -> dict[str, Any]:
        try:
            return self.trust.status(Path(workspace)) if workspace else {"projects": self.trust.list()}
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc

    def set_trusted(self, workspace: str) -> dict[str, Any]:
        try:
            status = self.trust.trust(Path(workspace))
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self.trace.record(
            "project.trust.changed",
            workspace=status["workspace"],
            data={
                "level": "trusted",
                "git_remote_hash": stable_hash(status["git_remote"]) if status["git_remote"] else "",
            },
        )
        return status

    def remove(self, workspace: str) -> dict[str, Any]:
        try:
            root = Path(workspace).expanduser().resolve()
            removed = self.trust.remove(root)
            status = self.trust.status(root)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self.trace.record(
            "project.trust.changed",
            workspace=str(root),
            data={"level": "untrusted", "removed": removed},
        )
        return {**status, "removed": removed}


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    cpus: str
    memory: str
    pids_limit: int


class ExecutionApplicationService:
    def __init__(
        self,
        execution: ExecutionRuntime,
        workspace: WorkspaceRuntime,
        limits: SandboxLimits,
    ) -> None:
        self.execution = execution
        self.workspace = workspace
        self.limits = limits

    async def execute(self, request: Any) -> dict[str, Any]:
        root = Path(request.workspace).expanduser().resolve()
        if not root.is_dir():
            raise InvalidRequest("workspace 不存在或不是目录")
        command = self.workspace.project_command(root, request.action)
        if not command:
            raise InvalidRequest(
                f"未能自动探测 {request.action} 命令，请在 .aicode/config.json 的 commands.{request.action} 中配置"
            )
        try:
            execution_request = ExecutionRequest(
                execution_id=request.execution_id,
                backend=request.backend,
                action=request.action,
                workspace=root,
                shell_command=command,
                allowed_roots=(root,),
                masked_paths=(".env*",),
                network="none",
                limits=ResourceLimits(
                    timeout_seconds=request.timeout_seconds,
                    cpus=self.limits.cpus,
                    memory=self.limits.memory,
                    pids_limit=self.limits.pids_limit,
                ),
            )
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        try:
            result = await self.execution.execute(execution_request)
        except ValueError as exc:
            raise Conflict(str(exc)) from exc
        payload = result.to_dict()
        payload["action"] = request.action
        return payload

    async def cancel(self, execution_id: str) -> dict[str, str]:
        cancelled = await self.execution.cancel(execution_id)
        return {"status": "cancelled" if cancelled else "idle", "execution_id": execution_id}


class ModelService:
    def __init__(self, model: ModelRuntime) -> None:
        self.model = model

    def routes(self) -> dict[str, Any]:
        return self.model.route_status()

    async def probe(self, *, model: str | None = None, tools: bool = True) -> dict[str, Any]:
        return await self.model.probe(model=model, tools=tools)
