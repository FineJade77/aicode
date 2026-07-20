from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from app.config.settings import AnthropicSettings
from app.models.provider import (
    RETRYABLE_STATUS,
    CompletionRequest,
    ProviderError,
    StreamEvent,
    ToolCallRequest,
    Usage,
    tool_argument_parse_error,
)

ANTHROPIC_VERSION = "2023-06-01"


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
        payload: dict[str, Any] = {
            "model": request.model,
            "system": request.system,
            "messages": to_anthropic_messages(request.messages),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
        }
        if request.tools:
            payload["tools"] = request.tools  # canonical schema 与 Anthropic 格式一致
        headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "Content-Type": "application/json"}
        url = self.settings.base_url.rstrip("/") + "/v1/messages"

        for attempt in range(3):
            yielded = False
            try:
                async with self.client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        if response.status_code in RETRYABLE_STATUS and attempt < 2:
                            raise _Retry(body)
                        raise ProviderError(f"anthropic HTTP {response.status_code}: {body}")
                    async for event in self._parse_stream(response, request.model):
                        yielded = True
                        yield event
                return
            except _Retry:
                await asyncio.sleep(0.5 * 2**attempt)
            except httpx.TransportError as exc:
                if yielded or attempt >= 2:
                    raise ProviderError(f"anthropic request failed: {exc}") from exc
                await asyncio.sleep(0.5 * 2**attempt)

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
                usage.input_tokens = int((message.get("usage") or {}).get("input_tokens") or 0)
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
    pass
