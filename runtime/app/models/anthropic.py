from __future__ import annotations

import asyncio
import copy
import json
import os
from typing import Any

import httpx

from app.config import AnthropicSettings
from app.models.provider import (
    RETRYABLE_STATUS,
    CompletionRequest,
    ContextOverflowError,
    ProviderError,
    StreamEvent,
    ToolCallRequest,
    Usage,
    backoff_delay,
    is_context_overflow_response,
    retry_after_seconds,
    tool_argument_parse_error,
)

ANTHROPIC_VERSION = "2023-06-01"

CACHE_CONTROL = {"type": "ephemeral"}


def cached_system(system: str) -> list[dict[str, Any]]:
    """Render the system prompt as one cacheable block.

    Anthropic renders `tools` -> `system` -> `messages`, so a breakpoint on the
    last system block covers the tool definitions as well. The separate tool
    breakpoint below is a second, earlier one: if the system prompt changes but
    the tool set does not, the tools stay cached instead of the whole prefix
    being rewritten.
    """
    return [{"type": "text", "text": system, "cache_control": dict(CACHE_CONTROL)}]


def cached_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy the tool list and mark its last element as a cache breakpoint.

    Deep-copied on purpose. `TOOL_SCHEMAS` is a module-level constant shared
    with the OpenAI-compatible provider; annotating it in place would attach an
    Anthropic-only `cache_control` key to every subsequent OpenAI request — a
    corruption that would surface far from here and only when both providers ran
    in one process.
    """
    if not tools:
        return []
    copied = copy.deepcopy(list(tools))
    copied[-1]["cache_control"] = dict(CACHE_CONTROL)
    return copied


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": str(message["content"])})
            for call in message.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]})
            mapped.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": str(message.get("content") or "")}
            if mapped and mapped[-1]["role"] == "user":
                mapped[-1]["content"].append(block)
            else:
                mapped.append({"role": "user", "content": [block]})
        else:
            block = {"type": "text", "text": str(message.get("content") or "")}
            if mapped and mapped[-1]["role"] == "user":
                mapped[-1]["content"].append(block)
            else:
                mapped.append({"role": "user", "content": [block]})
    return mapped


class AnthropicProvider:
    provider_name = "anthropic"

    def __init__(self, settings: AnthropicSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def api_key(self) -> str | None:
        return os.getenv(self.settings.api_key_env)

    def is_configured(self) -> bool:
        return bool(self.api_key())

    async def stream_complete(self, request: CompletionRequest):
        api_key = self.api_key()
        if not api_key:
            raise ProviderError(f"missing API key env: {self.settings.api_key_env}")
        caching = bool(getattr(self.settings, "prompt_caching", False))
        payload: dict[str, Any] = {
            "model": request.model,
            "system": cached_system(request.system) if caching else request.system,
            "messages": to_anthropic_messages(request.messages),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
        }
        if request.tools:
            # The canonical schema matches Anthropic's format.
            payload["tools"] = cached_tools(request.tools) if caching else request.tools
        headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "Content-Type": "application/json"}
        url = self.settings.base_url.rstrip("/") + "/v1/messages"

        for attempt in range(3):
            yielded = False
            try:
                async with self.client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        if is_context_overflow_response(response.status_code, body):
                            raise ContextOverflowError(f"anthropic HTTP {response.status_code}: {body}")
                        if response.status_code in RETRYABLE_STATUS and attempt < 2:
                            raise _Retry(retry_after_seconds(response.headers))
                        raise ProviderError(f"anthropic HTTP {response.status_code}: {body}")
                    async for event in self._parse_stream(response, request.model):
                        yielded = True
                        yield event
                return
            except _Retry as retry:
                # Honour Retry-After when present, otherwise back off with jitter
                # so concurrent clients do not retry in lockstep.
                await asyncio.sleep(retry.delay if retry.delay is not None else backoff_delay(attempt))
            except httpx.TransportError as exc:
                if yielded or attempt >= 2:
                    raise ProviderError(f"anthropic request failed: {exc}") from exc
                await asyncio.sleep(backoff_delay(attempt))

    async def _parse_stream(self, response, fallback_model: str):
        usage = Usage()
        model = fallback_model
        current_tool: dict[str, str] | None = None
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            chunk = json.loads(line[len("data: "):])
            kind = chunk.get("type")
            if kind == "message_start":
                message = chunk.get("message") or {}
                model = str(message.get("model") or model)
                reported = message.get("usage") or {}
                usage.input_tokens = int(reported.get("input_tokens") or 0)
                # Absent on a provider or model that does not cache, which is
                # why these read as 0 rather than raising: the fields are a
                # report about the request, not a promise the API makes.
                usage.cache_creation_input_tokens = int(reported.get("cache_creation_input_tokens") or 0)
                usage.cache_read_input_tokens = int(reported.get("cache_read_input_tokens") or 0)
            elif kind == "content_block_start":
                block = chunk.get("content_block") or {}
                if block.get("type") == "tool_use":
                    current_tool = {"id": str(block.get("id") or ""), "name": str(block.get("name") or ""), "json": ""}
            elif kind == "content_block_delta":
                delta = chunk.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield StreamEvent(type="text_delta", text=str(delta["text"]))
                elif delta.get("type") == "input_json_delta" and current_tool is not None:
                    current_tool["json"] += str(delta.get("partial_json") or "")
            elif kind == "content_block_stop" and current_tool is not None:
                raw_arguments = current_tool["json"]
                try:
                    arguments = json.loads(raw_arguments or "{}")
                except json.JSONDecodeError as exc:
                    arguments = tool_argument_parse_error(raw_arguments, exc)
                if not isinstance(arguments, dict):
                    arguments = tool_argument_parse_error(raw_arguments, ValueError("tool arguments JSON must be an object"))
                yield StreamEvent(type="tool_call", tool_call=ToolCallRequest(id=current_tool["id"], name=current_tool["name"], arguments=arguments))
                current_tool = None
            elif kind == "message_delta":
                usage.output_tokens = int((chunk.get("usage") or {}).get("output_tokens") or usage.output_tokens)
        yield StreamEvent(type="done", usage=usage, model=model)


class _Retry(Exception):
    """Retryable HTTP status, carrying the server's Retry-After if it sent one."""

    def __init__(self, delay: float | None = None) -> None:
        super().__init__("retryable provider response")
        self.delay = delay
