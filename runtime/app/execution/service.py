from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from app.execution.backend import ExecutionBackend
from app.execution.docker import DockerExecutionBackend
from app.execution.host import HostExecutionBackend
from app.execution.models import ExecutionRequest, ExecutionResult, ExecutionStatus


class ExecutionService:
    def __init__(
        self,
        *,
        audit: Any = None,
        host: HostExecutionBackend | None = None,
        docker: ExecutionBackend | None = None,
    ) -> None:
        self.audit = audit
        self.host = host or HostExecutionBackend()
        self.docker = docker or DockerExecutionBackend(self.host)
        self._active: dict[str, ExecutionBackend] = {}

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.execution_id in self._active:
            raise ValueError(f"execution_id 已存在: {request.execution_id}")
        backend: ExecutionBackend = self.host if request.backend == "host" else self.docker
        self._active[request.execution_id] = backend
        self._record("execution.started", request, None)
        try:
            result = await backend.execute(request)
        except asyncio.CancelledError:
            await backend.cancel(request.execution_id)
            self._record("execution.finished", request, _cancelled_result(request))
            raise
        except Exception as exc:
            result = ExecutionResult(
                execution_id=request.execution_id,
                backend=request.backend,
                status=ExecutionStatus.FAILED,
                exit_code=-1,
                stderr=f"{exc.__class__.__name__}: {exc}",
            )
        finally:
            self._active.pop(request.execution_id, None)
        self._record("execution.finished", request, result)
        return result

    async def cancel(self, execution_id: str) -> bool:
        backend = self._active.get(execution_id)
        if backend is None:
            return False
        return await backend.cancel(execution_id)

    async def cancel_all(self) -> None:
        for execution_id in list(self._active):
            await self.cancel(execution_id)

    def status(self) -> dict[str, Any]:
        return {"active": len(self._active)}

    def _record(self, event_type: str, request: ExecutionRequest, result: ExecutionResult | None) -> None:
        if self.audit is None:
            return
        data: dict[str, Any] = {
            "execution_id": request.execution_id,
            "backend": request.backend,
            "action": request.action,
            "executable": request.executable,
            "command_hash": hashlib.sha256(request.command_payload()).hexdigest(),
            "mode": request.mode,
            "run_id": request.run_id or None,
            "tool_call_id": request.tool_call_id or None,
            "network": request.network,
            "trust_level": request.trust_level,
            "timeout_seconds": request.limits.timeout_seconds,
            "cpus": request.limits.cpus,
            "memory": request.limits.memory,
            "pids_limit": request.limits.pids_limit,
            "writable_path_count": len(request.writable_paths),
            "masked_path_count": len(request.masked_paths),
        }
        if result is not None:
            data.update(
                {
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "timed_out": result.timed_out,
                    "cancelled": result.cancelled,
                }
            )
        self.audit.record(
            event_type,
            session_id=request.session_id or None,
            workspace=str(request.workspace),
            data=data,
        )


def _cancelled_result(request: ExecutionRequest) -> ExecutionResult:
    return ExecutionResult(
        execution_id=request.execution_id,
        backend=request.backend,
        status=ExecutionStatus.CANCELLED,
        exit_code=-1,
        cancelled=True,
    )
