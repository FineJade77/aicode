from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

import httpx

from app.config.settings import OpenAICompatibleSettings
from app.models.provider import (
    RETRYABLE_STATUS,
    CompletionRequest,
    ModelProvider,
    ModelProviderUnavailable,
    ModelRequest,
    ModelResponse,
    ProviderError,
    StreamEvent,
    ToolCallRequest,
    Usage,
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


class OpenAICompatibleProvider(ModelProvider):
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

    def api_key(self) -> str | None:
        direct = os.getenv("AICODE_OPENAI_API_KEY")
        if direct:
            return direct
        return os.getenv(self.settings.api_key_env)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        api_key = self.api_key()
        if not api_key:
            raise ModelProviderUnavailable(f"missing API key env: {self.settings.api_key_env}")

        model = request.model
        if not model:
            raise ValueError("model is required for OpenAI-compatible requests")

        payload: dict[str, Any] = {
            "model": model,
            "messages": request.messages,
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        response = await asyncio.to_thread(self._post_chat_completion, payload, api_key)
        return parse_chat_completion_response(response, fallback_model=model, provider=self.provider_name)

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
            try:
                arguments = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            yield StreamEvent(type="tool_call", tool_call=ToolCallRequest(id=slot["id"] or f"tc_{index}", name=slot["name"], arguments=arguments))
        yield StreamEvent(type="done", usage=usage, model=model)

    def _post_chat_completion(self, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=chat_completions_url(self.settings.base_url),
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI-compatible request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI-compatible request failed: {exc.reason}") from exc


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def parse_chat_completion_response(payload: dict[str, Any], *, fallback_model: str, provider: str) -> ModelResponse:
    choices = payload.get("choices") or []
    text = ""
    if choices:
        message = choices[0].get("message") or {}
        text = str(message.get("content") or "")

    usage = payload.get("usage") or {}
    return ModelResponse(
        text=text,
        model=str(payload.get("model") or fallback_model),
        provider=provider,
        input_tokens=as_int(usage.get("prompt_tokens")),
        output_tokens=as_int(usage.get("completion_tokens")),
        estimated_cost=0.0,
    )


def as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
