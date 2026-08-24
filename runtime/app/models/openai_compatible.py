from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx

from app.config import OpenAICompatibleSettings
from app.models.provider import (
    RETRYABLE_STATUS,
    CompletionRequest,
    ContextOverflowError,
    ProviderCapabilityError,
    ProviderError,
    ProviderNotConfigured,
    StreamEvent,
    ToolCallRequest,
    Usage,
    backoff_delay,
    is_context_overflow_response,
    retry_after_seconds,
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
    """Retryable HTTP status, carrying the server's Retry-After if it sent one."""

    def __init__(self, delay: float | None = None) -> None:
        super().__init__("retryable provider response")
        self.delay = delay


class OpenAICompatibleProvider:
    provider_name = "openai_compatible"

    def __init__(self, settings: OpenAICompatibleSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    def is_configured(self) -> bool:
        return self.settings.auth_mode != "required" or bool(self.api_key())

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

    def request_headers(self) -> dict[str, str]:
        api_key = self.api_key()
        if self.settings.auth_mode == "required" and not api_key:
            raise ProviderNotConfigured(f"missing API key env: {self.settings.api_key_env}")
        headers = {"Content-Type": "application/json"}
        if self.settings.auth_mode != "none" and api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def stream_complete(self, request: CompletionRequest):
        if not self.settings.streaming:
            raise ProviderCapabilityError(
                f"provider profile {self.settings.profile!r} declares streaming=false; aicode requires streaming"
            )
        if request.tools and not self.settings.tool_calling:
            raise ProviderCapabilityError(
                f"provider profile {self.settings.profile!r} declares tool_calling=false; refusing text JSON fallback"
            )
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
        headers = self.request_headers()
        url = chat_completions_url(self.settings.base_url)

        for attempt in range(3):
            yielded = False
            try:
                async with self.client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        if is_context_overflow_response(response.status_code, body):
                            raise ContextOverflowError(f"openai-compatible HTTP {response.status_code}: {body}")
                        if response.status_code in RETRYABLE_STATUS and attempt < 2:
                            raise _Retry(retry_after_seconds(response.headers))
                        raise ProviderError(f"openai-compatible HTTP {response.status_code}: {body}")
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
                    raise ProviderError(f"openai-compatible request failed: {exc}") from exc
                await asyncio.sleep(backoff_delay(attempt))

    async def probe(self, model: str, *, tools: bool = True) -> dict[str, Any]:
        started = time.monotonic()
        checks: list[dict[str, Any]] = []
        discovered_models: list[str] = []

        if self.settings.auth_mode == "required" and not self.api_key():
            checks.append(
                probe_check(
                    "configuration",
                    "fail",
                    "auth_required",
                    f"API key environment variable {self.settings.api_key_env} is missing",
                )
            )
            return probe_result(model, checks, discovered_models, started)
        checks.append(
            probe_check(
                "configuration",
                "pass",
                "configured",
                f"auth_mode={self.settings.auth_mode}",
            )
        )

        endpoint_started = time.monotonic()
        try:
            response = await self.client.get(models_url(self.settings.base_url), headers=self.request_headers())
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            checks.append(
                probe_check(
                    "endpoint",
                    "fail",
                    "endpoint_unreachable",
                    f"could not connect to the model discovery endpoint: {safe_exception_text(exc)}",
                    endpoint_started,
                )
            )
            return probe_result(model, checks, discovered_models, started)

        if response.status_code in {401, 403}:
            checks.append(
                probe_check(
                    "endpoint",
                    "fail",
                    "auth_failed",
                    f"model discovery endpoint rejected authentication (HTTP {response.status_code})",
                    endpoint_started,
                )
            )
            return probe_result(model, checks, discovered_models, started)
        if response.status_code >= 400:
            checks.append(
                probe_check(
                    "endpoint",
                    "fail",
                    "endpoint_http_error",
                    f"model discovery endpoint returned HTTP {response.status_code}",
                    endpoint_started,
                )
            )
            return probe_result(model, checks, discovered_models, started)

        try:
            payload = response.json()
            discovered_models = discovered_model_ids(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            checks.append(
                probe_check(
                    "endpoint",
                    "fail",
                    "invalid_models_response",
                    "model discovery endpoint did not return OpenAI-compatible JSON",
                    endpoint_started,
                )
            )
            return probe_result(model, checks, discovered_models, started)

        checks.append(
            probe_check(
                "endpoint",
                "pass",
                "reachable",
                f"discovered {len(discovered_models)} models",
                endpoint_started,
            )
        )
        if model not in discovered_models:
            checks.append(
                probe_check(
                    "model",
                    "fail",
                    "model_not_found",
                    f"configured model {model!r} is not present in the /models response",
                )
            )
            return probe_result(model, checks, discovered_models, started)
        checks.append(probe_check("model", "pass", "model_found", f"configured model {model!r} was discovered"))

        if not self.settings.streaming:
            checks.append(
                probe_check(
                    "streaming",
                    "fail",
                    "streaming_disabled",
                    "profile declares streaming=false, but the Agent runtime requires SSE streaming",
                )
            )
            return probe_result(model, checks, discovered_models, started)
        if tools and not self.settings.tool_calling:
            checks.append(
                probe_check(
                    "tools",
                    "fail",
                    "tools_disabled",
                    "profile declares tool_calling=false; aicode will not guess JSON from text",
                )
            )
            return probe_result(model, checks, discovered_models, started)

        probe_tools = (
            [
                {
                    "name": "aicode_probe",
                    "description": "Return provider capability probe acknowledgement.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                }
            ]
            if tools
            else []
        )
        request = CompletionRequest(
            purpose="probe",
            system="You are a provider capability probe. Follow the request exactly.",
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Call the aicode_probe tool exactly once with {\"ok\":true}. Do not answer with text."
                        if tools
                        else "Reply with exactly: AICODE_PROBE_OK"
                    ),
                }
            ],
            tools=probe_tools,
            model=model,
            temperature=0,
            max_tokens=32,
        )
        stream_started = time.monotonic()
        tool_calls: list[ToolCallRequest] = []
        done = False
        try:
            async for event in self.stream_complete(request):
                if event.type == "tool_call" and event.tool_call is not None:
                    tool_calls.append(event.tool_call)
                elif event.type == "done":
                    done = True
        except ProviderError as exc:
            code, summary = classify_completion_probe_error(exc)
            check_name = "tools" if tools and code == "tools_unsupported" else "streaming"
            checks.append(probe_check(check_name, "fail", code, summary, stream_started))
            return probe_result(model, checks, discovered_models, started)

        if not done:
            checks.append(
                probe_check(
                    "streaming",
                    "fail",
                    "stream_incomplete",
                    "SSE stream did not produce a terminal done event",
                    stream_started,
                )
            )
            return probe_result(model, checks, discovered_models, started)
        checks.append(probe_check("streaming", "pass", "stream_ok", "SSE streaming works", stream_started))

        if tools:
            matched = any(
                call.name == "aicode_probe" and call.arguments.get("ok") is True
                for call in tool_calls
            )
            if not matched:
                checks.append(
                    probe_check(
                        "tools",
                        "fail",
                        "tools_unsupported",
                        "model did not return the required native tool call; aicode will not guess JSON from text",
                    )
                )
                return probe_result(model, checks, discovered_models, started)
            checks.append(probe_check("tools", "pass", "tools_ok", "native tool calling works"))

        return probe_result(model, checks, discovered_models, started)

    async def _parse_stream(self, response, fallback_model: str):
        pending: dict[int, dict[str, Any]] = {}
        usage = Usage()
        model = fallback_model
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
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


def models_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    suffix = "/chat/completions"
    if base.endswith(suffix):
        base = base[: -len(suffix)]
    if base.endswith("/models"):
        return base
    return base + "/models"


def discovered_model_ids(payload: Any) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("models response must contain data array")
    models = {
        str(item["id"])
        for item in payload["data"]
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    return sorted(models)


def probe_check(
    name: str,
    status: str,
    code: str,
    summary: str,
    started: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "status": status, "code": code, "summary": summary}
    if started is not None:
        result["latency_ms"] = max(0, round((time.monotonic() - started) * 1_000))
    return result


def probe_result(
    model: str,
    checks: list[dict[str, Any]],
    discovered_models: list[str],
    started: float,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "error" if any(check["status"] == "fail" for check in checks) else "ok",
        "model": model,
        "discovered_models": discovered_models,
        "checks": checks,
        "latency_ms": max(0, round((time.monotonic() - started) * 1_000)),
    }


def classify_completion_probe_error(exc: ProviderError) -> tuple[str, str]:
    message = str(exc)
    normalized = message.casefold()
    if any(marker in normalized for marker in ("tool", "function")) and any(
        marker in normalized for marker in ("unsupported", "not support", "unknown", "invalid")
    ):
        return "tools_unsupported", f"endpoint rejected the tools request: {message}"
    if any(marker in normalized for marker in ("model not found", "unknown model", "does not exist")):
        return "model_not_found", f"completion endpoint could not find the configured model: {message}"
    if any(marker in normalized for marker in ("401", "403", "unauthorized", "forbidden")):
        return "auth_failed", f"completion endpoint authentication failed: {message}"
    return "streaming_failed", f"SSE completion probe failed: {message}"


def safe_exception_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text[:300] if text else exc.__class__.__name__


def as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
