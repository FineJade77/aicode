from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent.history import ContextManager, latest_valid_compaction
from app.agent.loop import AgentLoop
from app.agent.types import AgentRuntime
from app.application.contracts import (
    CompactionReceipt,
    RunControl,
    RunReceipt,
    SessionSnapshot,
    SteerReceipt,
    TurnRequest,
)
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

    async def create(self, workspace: str) -> SessionSnapshot:
        session = self.sessions.create(workspace=workspace)
        self.trace.record(
            "session.created",
            session_id=session.session_id,
            workspace=session.workspace,
        )
        await session.events.put(
            {
                "type": "session.created",
                "session_id": session.session_id,
                "workspace": session.workspace,
            }
        )
        return SessionSnapshot.from_session(session)

    def require(self, session_id: str) -> AgentSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise NotFound("session not found")
        return session

    def get(self, session_id: str) -> SessionSnapshot:
        return SessionSnapshot.from_session(self.require(session_id))

    def list(self, *, last: bool = False) -> list[SessionSnapshot] | SessionSnapshot | None:
        if not last:
            return [SessionSnapshot.from_mapping(item) for item in self.sessions.list()]
        session = self.sessions.last()
        return None if session is None else SessionSnapshot.from_session(session)

    def bind_turn(self, session: AgentSession, request: TurnRequest) -> TurnRequest:
        if not self.workspace.same_workspace(session.workspace, request.workspace):
            raise InvalidRequest("message workspace does not match session workspace")
        return request.bind(workspace=session.workspace)


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

    async def submit(self, session: AgentSession, request: TurnRequest) -> RunReceipt:
        configured = getattr(self.model.primary, "is_configured", None)
        if callable(configured) and not configured():
            raise ProviderUnavailable(
                "The model provider is not configured. Set an API key such as OPENAI_API_KEY or ANTHROPIC_API_KEY and retry."
            )
        was_running = session.agent_runner_active() or session.agent_queue.qsize() > 0
        session.events.set_default_after(session.events.last_event_id())
        queued = session.enqueue_agent_run(request)
        session.events.set_default_run_id(queued.run_id)
        queue_position = session.agent_queue.qsize()
        trace_data = {
            "mode": request.mode,
            "message_hash": stable_hash(request.message),
            "message_preview": request.message[:200],
            "run_id": queued.run_id,
            "queued": was_running,
            "queue_position": queue_position,
        }
        model = str(getattr(request, "model", "") or "").strip()
        if model:
            trace_data["model"] = model
        self.trace.record(
            "message.received",
            session_id=session.session_id,
            workspace=session.workspace,
            data=trace_data,
        )
        await self._emit_queued(session, queued, was_running, queue_position)
        self.ensure_runner(session)
        return RunReceipt(status="queued" if was_running else "accepted", run_id=queued.run_id)

    async def steer(self, session: AgentSession, message: str) -> SteerReceipt:
        guidance = message.strip()
        if not guidance:
            raise InvalidRequest("steer message must not be empty")
        run_id = session.current_run_id
        if not session.agent_runner_active() or run_id is None:
            raise Conflict("session has no active run to steer")
        if session.current_run_stage == "finalizing":
            raise Conflict("active run is already finalizing")
        pending = session.enqueue_steer(guidance)
        self.trace.record(
            "run.steer.queued",
            session_id=session.session_id,
            workspace=session.workspace,
            data={
                "run_id": run_id,
                "pending": pending,
                "message_hash": stable_hash(guidance),
                "message_preview": guidance[:200],
            },
        )
        await session.events.put(
            {
                "type": "run.steer.queued",
                "run_id": run_id,
                "status": "queued",
                "pending": pending,
                "message": "Steering guidance is queued and will be applied at the next AgentLoop safe boundary.",
            }
        )
        return SteerReceipt(run_id=run_id, pending=pending)

    def ensure_runner(self, session: AgentSession) -> None:
        if session.agent_runner_active():
            return
        session.agent_runner_task = asyncio.create_task(self._process_runs(session))

    async def cancel(self, session: AgentSession, *, resume_queued: bool = True) -> RunControl:
        task = session.agent_runner_task
        run_id = session.current_run_id
        if task is None or task.done() or run_id is None:
            return RunControl(status="idle", run_id=None, queued=session.agent_queue.qsize())

        session.mark_agent_progress("cancelling")
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        session.agent_runner_task = None
        session.drain_steers()

        for approval in session.expire_pending_approvals():
            await session.events.put(
                {
                    "type": "approval.expired",
                    "run_id": run_id,
                    "approval_id": approval.approval_id,
                    "kind": approval.kind,
                    "message": "The current run was cancelled and the pending approval expired.",
                }
            )

        message = "The current run was cancelled."
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
        return RunControl(status="cancelled", run_id=run_id, queued=queued)

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
                await session.events.put({"type": "run.started", "message": "Started the current run."})
                await self.agent_loop.run(session, queued.request)
            finally:
                expired_steers = session.drain_steers()
                if expired_steers:
                    self.trace.record(
                        "run.steer.expired",
                        session_id=session.session_id,
                        workspace=session.workspace,
                        data={
                            "run_id": queued.run_id,
                            "count": len(expired_steers),
                            "message_hashes": [stable_hash(message) for message in expired_steers],
                        },
                    )
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
                    "The run is queued until the previous run in this session finishes."
                    if was_running
                    else "The run was accepted and is ready to start."
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


class ContextService:
    def __init__(self, agent: AgentRuntime, trace: TraceSink) -> None:
        self.agent = agent
        self.trace = trace

    async def compact(self, session: AgentSession) -> CompactionReceipt:
        if session.agent_runner_active() or session.current_run_id is not None:
            raise Conflict("cannot compact a session while a run is active")
        before = latest_valid_compaction(session)
        manager = self.agent.context_manager or ContextManager(self.agent)
        self.agent.context_manager = manager
        await manager.prepare(
            session=session,
            purpose="main",
            system="Manual persistent session compaction.",
            tools=[],
            max_tokens=1_024,
            force=True,
        )
        after = latest_valid_compaction(session)
        compacted = after is not None and (
            before is None
            or after.compaction_id != before.compaction_id
            or after.created_at != before.created_at
        )
        status = "compacted" if compacted else "unchanged"
        self.trace.record(
            "context.compact.requested",
            session_id=session.session_id,
            workspace=session.workspace,
            data={
                "status": status,
                "compaction_id": after.compaction_id if after is not None else None,
            },
        )
        return CompactionReceipt(
            status=status,
            compaction=after.to_dict() if after is not None else None,
        )


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
            raise InvalidRequest("workspace does not exist or is not a directory")
        command = self.workspace.project_command(root, request.action)
        if not command:
            raise InvalidRequest(
                f"Could not auto-detect the {request.action} command. Configure commands.{request.action} in .aicode/config.json."
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
