from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class AgentRequest(Protocol):
    message: str
    mode: str
    workspace: str
    language: str


@dataclass(slots=True)
class AgentRuntime:
    model_router: Any
    tools: Any
    audit: Any
    policy: Any = None
