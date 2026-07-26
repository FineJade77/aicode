from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
TOOL_ARGUMENT_PARSE_ERROR_KEY = "__aicode_tool_argument_parse_error__"
TOOL_ARGUMENT_PARSE_ERROR_RAW_LIMIT = 1_000


def tool_argument_parse_error(raw_arguments: str, error: BaseException) -> dict[str, Any]:
    return {
        TOOL_ARGUMENT_PARSE_ERROR_KEY: {
            "error": str(error),
            "raw_arguments": raw_arguments[:TOOL_ARGUMENT_PARSE_ERROR_RAW_LIMIT],
            "truncated": len(raw_arguments) > TOOL_ARGUMENT_PARSE_ERROR_RAW_LIMIT,
        }
    }


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


@dataclass(frozen=True, slots=True)
class ModelCapability:
    provider: str
    model: str
    context_window: int
    max_output_tokens: int
    source: str = "default"
    tool_calling: bool = True
    streaming: bool = True
    tokenizer: str = "chars"
    chars_per_token: float = 3.5


class ProviderError(Exception):
    pass


class ContextOverflowError(ProviderError):
    pass


class ProviderNotConfigured(ProviderError):
    pass


class ProviderCapabilityError(ProviderError):
    pass


def is_context_overflow_response(status_code: int, body: str) -> bool:
    normalized = body.casefold()
    if status_code == 413:
        return True
    exact_markers = (
        "context_length_exceeded",
        "maximum context length",
        "context window",
        "prompt is too long",
        "input is too long",
        "too many input tokens",
        "request too large for model",
        "reduce the length of the messages",
    )
    if any(marker in normalized for marker in exact_markers):
        return True
    return status_code in {400, 413, 422} and "token" in normalized and any(
        marker in normalized for marker in ("limit", "maximum", "exceed", "too long")
    )


class StreamingModelProvider(Protocol):
    provider_name: str

    def is_configured(self) -> bool: ...

    def stream_complete(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]: ...
