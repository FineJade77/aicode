from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.config.settings import Settings
from app.models.anthropic import AnthropicProvider
from app.models.openai_compatible import OpenAICompatibleProvider
from app.models.provider import (
    CompletionRequest,
    CompletionResult,
    ModelCapability,
    ProviderNotConfigured,
    StreamingModelProvider,
    ToolCallRequest,
    Usage,
)
from app.usage.pricing import estimate_cost, model_prices_data


@dataclass(slots=True)
class ModelRouter:
    primary: StreamingModelProvider
    settings: Settings

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelRouter":
        if settings.provider.type == "anthropic":
            primary: StreamingModelProvider = AnthropicProvider(settings.anthropic)
        else:
            primary = OpenAICompatibleProvider(settings.openai_compatible)
        return cls(primary=primary, settings=settings)

    def model_for_purpose(self, purpose: str) -> str:
        if purpose == "reviewer":
            return self.settings.models.reviewer
        if purpose == "summarizer":
            return self.settings.models.summarizer
        return self.settings.models.main

    def capability_for_purpose(self, purpose: str) -> ModelCapability:
        model = self.model_for_purpose(purpose)
        provider = str(getattr(self.primary, "provider_name", self.primary.__class__.__name__))
        provider_key = f"{provider}:{model}"
        contexts = self.settings.context.model_context_windows
        outputs = self.settings.context.model_max_output_tokens
        context_window = contexts.get(provider_key, contexts.get(model, self.settings.context.default_context_window))
        max_output_tokens = outputs.get(provider_key, outputs.get(model, self.settings.context.default_max_output_tokens))
        source = "configured" if provider_key in contexts or model in contexts else "default"
        return ModelCapability(
            provider=provider,
            model=model,
            context_window=context_window,
            max_output_tokens=min(max_output_tokens, max(1, context_window - 1)),
            source=source,
        )

    async def aclose(self) -> None:
        aclose = getattr(self.primary, "aclose", None)
        if callable(aclose):
            await aclose()

    async def stream_complete(
        self,
        *,
        purpose: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | tuple = (),
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> CompletionResult:
        is_configured = getattr(self.primary, "is_configured", None)
        if callable(is_configured) and not is_configured():
            raise ProviderNotConfigured("模型 provider 未配置，请设置 API key 后重试")
        request = CompletionRequest(
            purpose=purpose,
            system=system,
            messages=messages,
            tools=list(tools),
            model=self.model_for_purpose(purpose),
            temperature=temperature,
            max_tokens=min(max_tokens, self.capability_for_purpose(purpose).max_output_tokens),
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        usage = Usage()
        model_name = request.model
        async for event in self.primary.stream_complete(request):
            if event.type == "text_delta":
                text_parts.append(event.text)
                if on_text_delta is not None:
                    await on_text_delta(event.text)
            elif event.type == "tool_call" and event.tool_call is not None:
                tool_calls.append(event.tool_call)
            elif event.type == "done":
                usage = event.usage or usage
                model_name = event.model or model_name
        provider_name = getattr(self.primary, "provider_name", self.primary.__class__.__name__)
        return CompletionResult(
            text="".join(text_parts),
            tool_calls=tool_calls,
            model=model_name,
            provider=provider_name,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost=estimate_cost(
                provider=provider_name,
                model=model_name,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                prices=self.settings.pricing.model_prices,
            ),
        )

    def route_status(self) -> dict:
        primary_name = getattr(self.primary, "provider_name", self.primary.__class__.__name__)
        is_configured = getattr(self.primary, "is_configured", None)
        primary_configured = bool(is_configured()) if callable(is_configured) else True
        return {
            "provider": {
                "primary": primary_name,
                "primary_configured": primary_configured,
                "type": self.settings.provider.type,
            },
            "routes": {
                "main": self.settings.models.main,
                "reviewer": self.settings.models.reviewer,
                "summarizer": self.settings.models.summarizer,
            },
            "capabilities": {
                purpose: {
                    "provider": capability.provider,
                    "model": capability.model,
                    "context_window": capability.context_window,
                    "max_output_tokens": capability.max_output_tokens,
                    "source": capability.source,
                }
                for purpose in ("main", "reviewer", "summarizer")
                for capability in (self.capability_for_purpose(purpose),)
            },
            "openai_compatible": {
                "base_url": self.settings.openai_compatible.base_url,
                "api_key_env": self.settings.openai_compatible.api_key_env,
                "timeout_seconds": self.settings.openai_compatible.timeout_seconds,
            },
            "pricing": {
                "currency": self.settings.pricing.currency,
                "unit": "per_1m_tokens",
                "models": model_prices_data(self.settings.pricing.model_prices),
            },
        }
