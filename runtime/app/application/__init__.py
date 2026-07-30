"""Public Application Runtime surface with side-effect-free lazy exports."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "APPLICATION_CONTRACT_VERSION": ("app.application.contracts", "APPLICATION_CONTRACT_VERSION"),
    "AgentRunState": ("app.application.contracts", "AgentRunState"),
    "CompactionReceipt": ("app.application.contracts", "CompactionReceipt"),
    "RunControl": ("app.application.contracts", "RunControl"),
    "RunReceipt": ("app.application.contracts", "RunReceipt"),
    "SessionSnapshot": ("app.application.contracts", "SessionSnapshot"),
    "SteerReceipt": ("app.application.contracts", "SteerReceipt"),
    "TurnRequest": ("app.application.contracts", "TurnRequest"),
    "ApplicationRuntime": ("app.application.runtime", "ApplicationRuntime"),
    "ApprovalService": ("app.application.services", "ApprovalService"),
    "ContextService": ("app.application.services", "ContextService"),
    "ExecutionApplicationService": ("app.application.services", "ExecutionApplicationService"),
    "ModelService": ("app.application.services", "ModelService"),
    "ProjectTrustService": ("app.application.services", "ProjectTrustService"),
    "RunCoordinator": ("app.application.services", "RunCoordinator"),
    "SessionService": ("app.application.services", "SessionService"),
    "TraceService": ("app.application.services", "TraceService"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
