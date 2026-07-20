from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from app.config.settings import OpenAICompatibleSettings
from app.models.provider import (
    RETRYABLE_STATUS,
    CompletionRequest,
    ProviderError,
    StreamEvent,
    ToolCallRequest,
    Usage,
    tool_argument_parse_error,
)


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.get("content") or None}
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)},
                    }
                    for call in tool_calls
                ]
            mapped.append(entry)
        elif role == "tool":
            mapped.append({"role": "tool", "tool_call_id": message["tool_call_id"], "content": str(message.get("content") or "")})
        else:
            mapped.append({"role": "user", "content": str(message.get("content") or "")})
    return mapped


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}}
        for tool in tools
    ]


class _Retry(Exception):
    pass


class OpenAICompatibleProvider:
    provider_name = "openai_compatible"

    def __init__(self, settings: OpenAICompatibleSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    def is_configured(self) -> bool:
        return bool(self.api_key())

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
        direct = os.getenv("AICODE_OPENAI_API_KEY")
        if direct:
            return direct
        return os.getenv(self.settings.api_key_env)

    async def stream_complete(self, request: CompletionRequest):
        api_key = self.api_key()
        if not api_key:
            raise ProviderError(f"missing API key env: {self.settings.api_key_env}")
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": to_openai_messages(request.system, request.messages),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            payload["tools"] = to_openai_tools(request.tools)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        url = chat_completions_url(self.settings.base_url)

        for attempt in range(3):
            yielded = False
            try:
                async with self.client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        if response.status_code in RETRYABLE_STATUS and attempt < 2:
                            raise _Retry(body)
                        raise ProviderError(f"openai-compatible HTTP {response.status_code}: {body}")
                    async for event in self._parse_stream(response, request.model):
                        yielded = True
                        yield event
                return
            except _Retry:
                await asyncio.sleep(0.5 * 2**attempt)
            except httpx.TransportError as exc:
                if yielded or attempt >= 2:
                    raise ProviderError(f"openai-compatible request failed: {exc}") from exc
                await asyncio.sleep(0.5 * 2**attempt)

    async def _parse_stream(self, response, fallback_model: str):
        pending: dict[int, dict[str, Any]] = {}
        usage = Usage()
        model = fallback_model
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: "):].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            model = str(chunk.get("model") or model)
            raw_usage = chunk.get("usage")
            if isinstance(raw_usage, dict):
                usage = Usage(input_tokens=as_int(raw_usage.get("prompt_tokens")), output_tokens=as_int(raw_usage.get("completion_tokens")))
            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices else {}
            content = delta.get("content")
            if content:
                yield StreamEvent(type="text_delta", text=str(content))
            for raw_call in delta.get("tool_calls") or []:
                index = as_int(raw_call.get("index"))
                slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if raw_call.get("id"):
                    slot["id"] = str(raw_call["id"])
                function = raw_call.get("function") or {}
                if function.get("name"):
                    slot["name"] = str(function["name"])
                slot["arguments"] += str(function.get("arguments") or "")
        for index in sorted(pending):
            slot = pending[index]
            raw_arguments = str(slot["arguments"] or "")
            try:
                arguments = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError as exc:
                arguments = tool_argument_parse_error(raw_arguments, exc)
            if not isinstance(arguments, dict):
                arguments = tool_argument_parse_error(raw_arguments, ValueError("tool arguments JSON must be an object"))
            yield StreamEvent(type="tool_call", tool_call=ToolCallRequest(id=slot["id"] or f"tc_{index}", name=slot["name"], arguments=arguments))
        yield StreamEvent(type="done", usage=usage, model=model)


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
