from __future__ import annotations

from typing import Protocol

from app.execution.models import ExecutionRequest, ExecutionResult


class ExecutionBackend(Protocol):
    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...

    async def cancel(self, execution_id: str) -> bool: ...

    async def cancel_all(self) -> None: ...
