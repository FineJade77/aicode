from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.config.settings import Settings
from app.models.anthropic import AnthropicProvider
from app.models.openai_compatible import OpenAICompatibleProvider
from app.models.provider import (
    CompletionRequest,
    CompletionResult,
    ModelProvider,
    ModelProviderUnavailable,
    ModelRequest,
    ModelResponse,
    ProviderNotConfigured,
    StubProvider,
    ToolCallRequest,
    Usage,
)
from app.usage.pricing import estimate_cost, model_prices_data


@dataclass(slots=True)
class ModelRouter:
    primary: ModelProvider
    fallback: ModelProvider
    settings: Settings

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelRouter":
        if settings.provider.type == "anthropic":
            primary: ModelProvider = AnthropicProvider(settings.anthropic)
        else:
            primary = OpenAICompatibleProvider(settings.openai_compatible)
        return cls(primary=primary, fallback=StubProvider(), settings=settings)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        routed = ModelRequest(
            purpose=request.purpose,
            messages=request.messages,
            model=request.model or self.model_for_purpose(request.purpose),
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )

        try:
            return self.with_estimated_cost(await self.primary.complete(routed))
        except ModelProviderUnavailable:
            return self.with_estimated_cost(
                await self.fallback.complete(
                    ModelRequest(
                        purpose=routed.purpose,
                        messages=routed.messages,
                        model="stub",
                        temperature=routed.temperature,
                        max_tokens=routed.max_tokens,
                    )
                )
            )

    def model_for_purpose(self, purpose: str) -> str:
        if purpose == "planner":
            return self.settings.models.planner
        if purpose == "coder":
            return self.settings.models.coder
        if purpose == "reviewer":
            return self.settings.models.reviewer
        if purpose == "summarizer":
            return self.settings.models.summarizer
        return self.settings.models.default

    def model_for_purpose_v2(self, purpose: str) -> str:
        if purpose == "reviewer":
            return self.settings.models.reviewer
        if purpose == "summarizer":
            return self.settings.models.summarizer
        return self.settings.models.main

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
            model=self.model_for_purpose_v2(purpose),
            temperature=temperature,
            max_tokens=max_tokens,
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

    def with_estimated_cost(self, response: ModelResponse) -> ModelResponse:
        response.estimated_cost = estimate_cost(
            provider=response.provider,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            prices=self.settings.pricing.model_prices,
        )
        return response

    def route_status(self) -> dict:
        primary_name = getattr(self.primary, "provider_name", self.primary.__class__.__name__)
        fallback_name = getattr(self.fallback, "provider_name", self.fallback.__class__.__name__)
        is_configured = getattr(self.primary, "is_configured", None)
        primary_configured = bool(is_configured()) if callable(is_configured) else True
        return {
            "provider": {
                "primary": primary_name,
                "primary_configured": primary_configured,
                "fallback": fallback_name,
            },
            "routes": {
                "default": self.settings.models.default,
                "planner": self.settings.models.planner,
                "coder": self.settings.models.coder,
                "reviewer": self.settings.models.reviewer,
                "summarizer": self.settings.models.summarizer,
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
