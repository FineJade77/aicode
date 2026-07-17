from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass(slots=True)
class ModelRequest:
    purpose: str
    messages: list[dict[str, str]]
    model: str | None = None
    temperature: float = 0.2
    max_tokens: int | None = None


@dataclass(slots=True)
class ModelResponse:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0


class ModelProviderUnavailable(Exception):
    pass


class ModelProvider:
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError


class StubProvider(ModelProvider):
    provider_name = "stub"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="Runtime 骨架已连接。模型 provider 后续接入 OpenAI-compatible API。",
            model=request.model or "stub",
            provider="stub",
            input_tokens=sum(len(message.get("content", "")) for message in request.messages),
            output_tokens=18,
            estimated_cost=0.0,
        )


@dataclass(slots=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class CompletionRequest:
    purpose: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 8192


@dataclass(slots=True)
class StreamEvent:
    type: str  # "text_delta" | "tool_call" | "done"
    text: str = ""
    tool_call: ToolCallRequest | None = None
    usage: Usage | None = None
    model: str = ""


@dataclass(slots=True)
class CompletionResult:
    text: str
    tool_calls: list[ToolCallRequest]
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0


class ProviderError(Exception):
    pass


class ProviderNotConfigured(ProviderError):
    pass


class StreamingModelProvider(Protocol):
    provider_name: str

    def is_configured(self) -> bool: ...

    def stream_complete(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]: ...
