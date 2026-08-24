from __future__ import annotations

import asyncio
import tempfile
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.agent.history import ContextManager, context_status, latest_valid_compaction
from app.agent.loop import AgentLoop
from app.agent.ports import (
    Clock,
    ExecutionRuntime,
    ModelRuntime,
    ProjectTrustRepository,
    SessionRepository,
    TraceSink,
    UsageRuntime,
    WorkspaceRuntime,
)
from app.agent.session import AgentSession
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
from app.execution import ExecutionRequest, ResourceLimits
from app.execution.artifacts import collect_artifacts
from app.security import stable_hash


class SessionService:
    def __init__(
        self,
        sessions: SessionRepository,
        trace: TraceSink,
        workspace: WorkspaceRuntime,
        agent: AgentRuntime | None = None,
    ) -> None:
        self.sessions = sessions
        self.trace = trace
        self.workspace = workspace
        # Optional so an embedder can build a session service without a model
        # runtime; the snapshot then omits the context block rather than
        # reporting zeros, which would read as "empty history".
        self.agent = agent

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

    async def fork(self, session_id: str, *, message_id: int | None = None) -> SessionSnapshot:
        self.require(session_id)
        try:
            forked = self.sessions.fork(session_id, message_id=message_id)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        self.trace.record(
            "session.forked",
            session_id=forked.session_id,
            workspace=forked.workspace,
            data={"source_session_id": session_id, "message_id": message_id},
        )
        await forked.events.put(
            {
                "type": "session.created",
                "session_id": forked.session_id,
                "workspace": forked.workspace,
                "forked_from": session_id,
            }
        )
        return SessionSnapshot.from_session(forked)

    def require(self, session_id: str) -> AgentSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise NotFound("session not found")
        return session

    def get(self, session_id: str) -> SessionSnapshot:
        session = self.require(session_id)
        snapshot = SessionSnapshot.from_session(session)
        if self.agent is None or self.agent.model_runtime is None:
            return snapshot
        return replace(snapshot, context=context_status(self.agent, session))

    def list(
        self,
        *,
        last: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SessionSnapshot] | SessionSnapshot | None:
        if not last:
            return [
                SessionSnapshot.from_mapping(item)
                for item in self.sessions.list(limit=limit, offset=offset)
            ]
        session = self.sessions.last()
        return None if session is None else SessionSnapshot.from_session(session)

    def prune(self, *, max_sessions: int | None = None, max_age_days: int | None = None) -> dict[str, Any]:
        result = self.sessions.prune(max_sessions=max_sessions, max_age_days=max_age_days)
        self.trace.record(
            "session.pruned",
            data={
                "status": result["status"],
                "deleted_sessions": result["deleted_sessions"],
                "deleted_messages": result["deleted_messages"],
                "retained_live": result["retained_live"],
                "max_sessions": max_sessions,
                "max_age_days": max_age_days,
            },
        )
        return result

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

    async def _record_cancelled_model_call(
        self,
        session: AgentSession,
        run_id: str,
        record: dict[str, Any] | None,
    ) -> None:
        """Account for a model call the cancellation cut off.

        Providers report usage once, in the final frame of the stream. Cancel
        before it arrives and the tokens already generated are billed but never
        recorded, so `aicode runtime usage` reports a total that is confidently
        short — the same failure as a call priced at $0.00 because its model was
        missing from the price table.

        Written from here rather than from the loop because the loop's task is
        already cancelled: every `await` inside it raises immediately, so it
        cannot emit its own epitaph.

        The token fields stay zero because zero is what is known. `complete:
        false` is the part that carries meaning: it marks the total as a lower
        bound instead of quietly folding an unknown into it.
        """
        if record is None:
            return
        payload = {
            "run_id": run_id,
            "purpose": record.get("purpose") or "unknown",
            "model": record.get("model") or "unknown",
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost": 0.0,
            "complete": False,
            "reason": "run_cancelled",
            # Characters actually streamed before the cut. Evidence that the
            # call was not free, not a substitute for the token count.
            "streamed_chars": int(record.get("streamed_chars") or 0),
        }
        self.trace.record(
            "usage.recorded",
            session_id=session.session_id,
            workspace=session.workspace,
            data=payload,
        )
        await session.events.put({"type": "usage.recorded", **payload})

    async def cancel(self, session: AgentSession, *, resume_queued: bool = True) -> RunControl:
        task = session.agent_runner_task
        run_id = session.current_run_id
        if task is None or task.done() or run_id is None:
            return RunControl(status="idle", run_id=None, queued=session.agent_queue.qsize())

        session.mark_agent_progress("cancelling")
        # Snapshot before cancelling: the runner clears this in its `finally`,
        # which runs while we await the cancelled task below.
        in_flight = session.end_model_call()
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        session.agent_runner_task = None
        session.drain_steers()
        await self._record_cancelled_model_call(session, run_id, in_flight)

        for approval in session.expire_pending_approvals():
            await session.events.put(
                {
                    "type": "approval.expired",
                    "run_id": run_id,
                    "approval_id": approval.approval_id,
                    "kind": approval.kind,
                    # Distinguishes cancellation from an approval that simply went
                    # unanswered; the broker emits reason="timeout" for that case.
                    "reason": "run_cancelled",
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
        selection: list[str] | None = None,
        guidance: str = "",
    ) -> dict[str, str]:
        # Four outcomes, not two. `selection` names the subset a partial
        # acceptance applies to; `guidance` turns a refusal into "not like that,
        # do X", which the model can act on where a bare no only stops it.
        resolution = ""
        if accepted and selection:
            resolution = "partial"
        elif not accepted and guidance.strip():
            resolution = "revise"
        if accept_all and accepted:
            session.auto_accept_edits = True
            self.trace.record(
                "approval.accept_all_enabled",
                session_id=session.session_id,
                workspace=session.workspace,
                data={"approval_id": approval_id},
            )
        if not session.resolve_approval(
            approval_id,
            accepted=accepted,
            resolution=resolution,
            response=guidance.strip(),
            selection=tuple(selection or ()),
        ):
            raise NotFound("approval not found or already resolved")
        status = resolution or ("accepted" if accepted else "rejected")
        self.trace.record(
            "approval.resolved",
            session_id=session.session_id,
            workspace=session.workspace,
            data={
                "approval_id": approval_id,
                "accepted": accepted,
                "resolution": status,
                # The count, not the paths: which files a user picked is not
                # something the audit needs, and the paths are already in the
                # edit events.
                "selected": len(selection or ()),
                "guided": bool(guidance.strip()),
            },
        )
        return {"status": status, "approval_id": approval_id}

    def answer(
        self,
        session: AgentSession,
        approval_id: str,
        *,
        answer: str,
    ) -> dict[str, str]:
        """Resolve a pending question with the user's text.

        Separate from `resolve` because the two carry different information: an
        approval is a decision, an answer is content. Routing answers through the
        boolean endpoint would leave no place for the text.
        """
        if not session.resolve_approval(approval_id, accepted=True, resolution="answered", response=answer):
            raise NotFound("question not found or already answered")
        self.trace.record(
            "question.answered",
            session_id=session.session_id,
            workspace=session.workspace,
            # The answer itself is user content and is deliberately not recorded
            # in the trace; only that one was given, and how long it was.
            data={"approval_id": approval_id, "answer_chars": len(answer)},
        )
        return {"status": "answered", "approval_id": approval_id}


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
        if getattr(request, "artifacts", False):
            with tempfile.TemporaryDirectory(prefix="aicode-artifacts-") as drop:
                return await self._execute_in(request, root, Path(drop))
        return await self._execute_in(request, root, None)

    async def _execute_in(self, request: Any, root: Path, artifact_root: Path | None) -> dict[str, Any]:
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
                artifact_root=artifact_root,
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
        if artifact_root is not None:
            # Collected here rather than in the backend so the drop is read once,
            # after the container is gone and nothing can still be writing to it.
            artifacts, truncated = collect_artifacts(artifact_root)
            result = replace(result, artifacts=artifacts, artifacts_truncated=truncated)
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

    async def probe_routes(self) -> dict[str, Any]:
        return await self.model.probe_routes()
