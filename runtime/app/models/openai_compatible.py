from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

from app.config.settings import OpenAICompatibleSettings
from app.models.provider import ModelProvider, ModelProviderUnavailable, ModelRequest, ModelResponse


class OpenAICompatibleProvider(ModelProvider):
    provider_name = "openai_compatible"

    def __init__(self, settings: OpenAICompatibleSettings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(self.api_key())

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
