from __future__ import annotations

import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRY_BASE_DELAY_SECONDS = 0.5
RETRY_MAX_DELAY_SECONDS = 8.0
# A server-supplied Retry-After is honoured, but not without bound: a provider
# advertising a multi-minute wait should surface as an error the user can react
# to rather than a silently hung request.
RETRY_AFTER_CAP_SECONDS = 60.0


def backoff_delay(attempt: int, *, random_source: Any = None) -> float:
    """Exponential backoff with equal jitter.

    Plain `base * 2**attempt` makes every client that hit the same rate limit
    retry in lockstep, re-creating the burst that caused it. Jitter spreads them
    out; the lower half is kept so a retry still waits a sensible minimum
    instead of hammering immediately.
    """
    ceiling = min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * (2**attempt))
    generator = random_source or random
    return generator.uniform(ceiling / 2, ceiling)


def retry_after_seconds(headers: Any) -> float | None:
    """Parse a Retry-After header in either delay-seconds or HTTP-date form."""
    if headers is None:
        return None
    raw = ""
    try:
        raw = str(headers.get("retry-after") or "").strip()
    except AttributeError:
        return None
    if not raw:
        return None
    try:
        return _capped(float(raw))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        # A malformed header must fall back to jittered backoff. Letting this
        # raise would turn a retryable 429 into an unhandled provider crash.
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _capped((parsed - datetime.now(UTC)).total_seconds())


def _capped(seconds: float) -> float | None:
    if seconds <= 0:
        return None
    return min(seconds, RETRY_AFTER_CAP_SECONDS)


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
    # `input_tokens` is the *uncached remainder*, not the whole prompt: the
    # provider reports cache-written and cache-read tokens separately, and the
    # prompt size is the sum of all three. Summing only `input_tokens` across a
    # cached session under-reports it by whatever the cache served.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


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
